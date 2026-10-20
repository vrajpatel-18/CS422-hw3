CLANG ?= clang
BPFFS ?= /sys/fs/bpf

all: dart.bpf.o

vmlinux.h:
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > $@

dart.bpf.o: dart.bpf.c vmlinux.h
	$(CLANG) -O2 -g -Wall -Wno-missing-declarations -target bpf $(if $(DEBUG),-DDEBUG) -c $< -o $@

# Registers "dart" with the kernel; the pinned link keeps it loaded after bpftool exits.
load: dart.bpf.o
	sudo bpftool struct_ops register $< $(BPFFS)
	@cat /proc/sys/net/ipv4/tcp_available_congestion_control

unload:
	sudo rm -f $(BPFFS)/dart
	@cat /proc/sys/net/ipv4/tcp_available_congestion_control

clean:
	rm -f dart.bpf.o vmlinux.h

.PHONY: all load unload clean
