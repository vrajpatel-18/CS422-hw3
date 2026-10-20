// SPDX-License-Identifier: GPL-2.0
/* DART: delay-aware, rate-targeting TCP congestion control (Assignment 2, Q3(b)). */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

char _license[] SEC("license") = "GPL";

#define TCP_INFINITE_SSTHRESH	0x7fffffff
#define TCP_CONG_NON_RESTRICTED	0x1
#define USEC_PER_SEC		1000000ULL

#define ALPHA		2	/* target backlog in the bottleneck queue, packets */
#define BETA		4
#define MIN_CWND	4
#define BW_BUCKET_ROUNDS 5	/* btl_bw = max of two 5-round buckets */
#define MIN_RTT_WIN_US	(10 * USEC_PER_SEC)
#define PROBE_RTT_US	200000

#ifdef DEBUG	/* make DEBUG=1; read with: sudo cat /sys/kernel/tracing/trace_pipe */
#define dbg(fmt, args...) bpf_printk(fmt, ##args)
#else
#define dbg(fmt, args...)
#endif

#define BPF_STRUCT_OPS(name, args...) SEC("struct_ops/" #name) BPF_PROG(name, ##args)

enum { STARTUP, CRUISE, COMPETE, PROBE_RTT };

struct dart {
	u32 min_rtt_us;
	u32 min_rtt_stamp;	/* times are tcp_mstamp in us, truncated to u32 */
	u32 round_stamp;
	u32 bw_cur;		/* max delivery rate (packets/s) in the current bucket */
	u32 bw_prev;
	u32 prev_btl_bw;
	u32 full_bw;		/* STARTUP: btl_bw at the last >= 25% growth */
	u32 loss_cwnd;		/* cwnd before the last loss, for undo */
	u32 prior_cwnd;		/* restored after PROBE_RTT */
	u32 probe_end;
	u32 probe_min_rtt;
	u32 round_min_rtt;	/* lowest RTT sample this round; filters jitter */
	u8 mode;
	u8 prior_mode;
	u8 bw_rounds;
	u8 full_bw_cnt;
	u8 high_rounds;		/* consecutive rounds with queued > BETA */
	u8 low_rounds;		/* COMPETE: consecutive rounds with queued < ALPHA */
};

static struct tcp_sock *tcp_sk(const struct sock *sk)
{
	return (struct tcp_sock *)sk;
}

static struct dart *dart_ca(const struct sock *sk)
{
	return (void *)((struct inet_connection_sock *)sk)->icsk_ca_priv;
}

static bool cwnd_limited(const struct tcp_sock *tp)
{
	if (tp->snd_cwnd < tp->snd_ssthresh)
		return tp->snd_cwnd < 2 * tp->max_packets_out;
	return BPF_CORE_READ_BITFIELD(tp, is_cwnd_limited);
}

static void set_cwnd(struct tcp_sock *tp, u32 cwnd)
{
	if (cwnd < MIN_CWND)
		cwnd = MIN_CWND;
	if (cwnd > tp->snd_cwnd_clamp)
		cwnd = tp->snd_cwnd_clamp;
	tp->snd_cwnd = cwnd;
}

static u32 btl_bw(const struct dart *d)
{
	return d->bw_cur > d->bw_prev ? d->bw_cur : d->bw_prev;
}

static u32 bdp_pkts(const struct dart *d)
{
	if (d->min_rtt_us == ~0U)
		return 0;
	return (u64)btl_bw(d) * d->min_rtt_us / USEC_PER_SEC;
}

/* Packets in the bottleneck queue: cwnd * qdelay / rtt, ignoring delay within rttvar (jitter). */
static u32 queued_pkts(const struct tcp_sock *tp, const struct dart *d, u32 rtt, u32 rttvar)
{
	if (d->min_rtt_us == ~0U || rtt <= d->min_rtt_us + rttvar)
		return 0;
	return (u64)tp->snd_cwnd * (rtt - d->min_rtt_us - rttvar) / rtt;
}

/* CUBIC HyStart's delay threshold: clamp(min_rtt / 8, 4 ms, 16 ms). */
static u32 startup_delay_thresh(const struct dart *d)
{
	u32 t = d->min_rtt_us / 8;

	return t < 4000 ? 4000 : t > 16000 ? 16000 : t;
}

void BPF_STRUCT_OPS(dart_init, struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct dart *d = dart_ca(sk);
	u32 now = (u32)tp->tcp_mstamp;

	__builtin_memset(d, 0, sizeof(*d));
	d->min_rtt_us = ~0U;
	d->min_rtt_stamp = now;
	d->round_stamp = now;
	d->mode = STARTUP;
	tp->snd_ssthresh = TCP_INFINITE_SSTHRESH;
}

void BPF_STRUCT_OPS(dart_pkts_acked, struct sock *sk, const struct ack_sample *sample)
{
	struct dart *d = dart_ca(sk);
	s32 rtt = sample->rtt_us;

	if (rtt <= 0)
		return;
	if (!d->round_min_rtt || (u32)rtt < d->round_min_rtt)
		d->round_min_rtt = rtt;
	if ((u32)rtt <= d->min_rtt_us) {
		d->min_rtt_us = rtt;
		d->min_rtt_stamp = (u32)tcp_sk(sk)->tcp_mstamp;
	}
	if (d->mode == PROBE_RTT && (!d->probe_min_rtt || (u32)rtt < d->probe_min_rtt))
		d->probe_min_rtt = rtt;
}

/* Runs once per smoothed RTT. */
static void dart_round(struct sock *sk, struct dart *d, u32 now, u32 srtt)
{
	struct tcp_sock *tp = tcp_sk(sk);
	u32 rtt = d->round_min_rtt ? d->round_min_rtt : srtt;
	u32 rttvar = tp->mdev_us >> 2, cwnd = tp->snd_cwnd, cap;
	u32 bw = btl_bw(d), bdp = bdp_pkts(d), queued = queued_pkts(tp, d, rtt, rttvar);
	bool limited = cwnd_limited(tp);

	d->round_min_rtt = 0;

	switch (d->mode) {
	case STARTUP:
		if (bw >= d->full_bw + d->full_bw / 4) {
			d->full_bw = bw;
			d->full_bw_cnt = 0;
		} else {
			d->full_bw_cnt++;
		}
		if (d->full_bw_cnt >= 3 || rtt > d->min_rtt_us + startup_delay_thresh(d)) {
			cwnd = bdp + ALPHA;	/* pipe full: drop the startup queue */
			d->mode = CRUISE;
		}
		break;
	case CRUISE:
		if (queued < ALPHA) {
			if (limited)
				cwnd += bw > d->prev_btl_bw ? 2 : 1;
		} else if (queued > BETA) {
			cwnd -= 1;
		}
		cap = bdp + (u64)bw * rttvar / USEC_PER_SEC + 4 * BETA;
		if (cwnd > cap)
			cwnd = cap;
		d->high_rounds = queued > BETA ? d->high_rounds + 1 : 0;
		if (d->high_rounds >= 8) {	/* someone else keeps the queue full */
			d->mode = COMPETE;
			d->low_rounds = 0;
		}
		break;
	case COMPETE:
		if (limited)
			cwnd += 1;
		d->low_rounds = queued < ALPHA ? d->low_rounds + 1 : 0;
		if (d->low_rounds >= 4) {
			d->mode = CRUISE;
			d->high_rounds = 0;
		}
		break;
	}
	set_cwnd(tp, cwnd);
	dbg("dart m=%u cwnd=%u bdp=%u", d->mode, tp->snd_cwnd, bdp);
	dbg("dart  q=%u rtt=%u min=%u", queued, rtt, d->min_rtt_us);
	dbg("dart  var=%u bw=%u", rttvar, bw);
	if (d->mode != STARTUP)
		tp->snd_ssthresh = tp->snd_cwnd;
	d->prev_btl_bw = bw;

	if (++d->bw_rounds >= BW_BUCKET_ROUNDS) {
		d->bw_prev = d->bw_cur;
		d->bw_cur = 0;
		d->bw_rounds = 0;
	}

	if (d->mode != STARTUP && now - d->min_rtt_stamp > MIN_RTT_WIN_US) {
		d->prior_cwnd = tp->snd_cwnd;
		d->prior_mode = d->mode;
		d->mode = PROBE_RTT;
		d->probe_end = now + (srtt > PROBE_RTT_US ? srtt : PROBE_RTT_US);
		d->probe_min_rtt = 0;
		set_cwnd(tp, MIN_CWND);
	}
}

void BPF_STRUCT_OPS(dart_cong_avoid, struct sock *sk, u32 ack, u32 acked)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct dart *d = dart_ca(sk);
	u32 now = (u32)tp->tcp_mstamp, srtt = tp->srtt_us >> 3;

	if (tp->rate_interval_us) {
		u32 bw = (u64)tp->rate_delivered * USEC_PER_SEC / tp->rate_interval_us;

		if (bw > d->bw_cur)
			d->bw_cur = bw;
	}

	if (d->mode == PROBE_RTT) {
		set_cwnd(tp, MIN_CWND);
		if ((s32)(now - d->probe_end) >= 0) {
			if (d->probe_min_rtt)
				d->min_rtt_us = d->probe_min_rtt;
			d->min_rtt_stamp = now;
			d->mode = d->prior_mode;
			set_cwnd(tp, d->prior_cwnd);
		}
		return;
	}

	if (d->mode == STARTUP && cwnd_limited(tp))
		set_cwnd(tp, tp->snd_cwnd + acked);	/* doubles once per RTT */

	if (srtt && now - d->round_stamp >= srtt) {
		d->round_stamp = now;
		dart_round(sk, d, now, srtt);
	}
}

/* Called once when a loss episode (recovery) or an RTO starts. */
u32 BPF_STRUCT_OPS(dart_ssthresh, struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct dart *d = dart_ca(sk);
	u32 cwnd = tp->snd_cwnd, srtt = tp->srtt_us >> 3, rttvar = tp->mdev_us >> 2;
	u32 bdp = bdp_pkts(d), target;

	if (d->mode == PROBE_RTT) {
		cwnd = d->prior_cwnd;
		d->mode = d->prior_mode;
	}
	d->loss_cwnd = cwnd;
	if (d->min_rtt_us != ~0U && srtt > d->min_rtt_us + 2 * rttvar) {
		target = cwnd * 7 / 10;		/* queue was full: congestion */
	} else {
		target = cwnd * 9 / 10;		/* queue empty: likely random loss */
		if (bdp > cwnd)
			bdp = cwnd;
		if (target < bdp)
			target = bdp;
	}
	if (d->mode == STARTUP)
		d->mode = CRUISE;
	return target > MIN_CWND ? target : MIN_CWND;
}

void BPF_STRUCT_OPS(dart_set_state, struct sock *sk, __u8 new_state)
{
	struct dart *d = dart_ca(sk);

	if (new_state == TCP_CA_Loss) {		/* RTO: restart from STARTUP */
		d->mode = STARTUP;
		d->full_bw = 0;
		d->full_bw_cnt = 0;
		d->high_rounds = 0;
		d->low_rounds = 0;
	}
}

u32 BPF_STRUCT_OPS(dart_undo_cwnd, struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	u32 prior = dart_ca(sk)->loss_cwnd;

	return tp->snd_cwnd > prior ? tp->snd_cwnd : prior;
}

SEC(".struct_ops.link")
struct tcp_congestion_ops dart = {
	.init		= (void *)dart_init,
	.pkts_acked	= (void *)dart_pkts_acked,
	.cong_avoid	= (void *)dart_cong_avoid,
	.ssthresh	= (void *)dart_ssthresh,
	.set_state	= (void *)dart_set_state,
	.undo_cwnd	= (void *)dart_undo_cwnd,
	.flags		= TCP_CONG_NON_RESTRICTED,
	.name		= "dart",
};
