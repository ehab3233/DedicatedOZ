"""A pretend server's serial port, for ipmi_sim's SOL to connect to.

Prints a BIOS-ish POST screen and a Linux boot when the chassis powers on or
resets, then a login prompt that echoes and answers a few commands. Enough to
exercise a real SOL session end to end, ANSI colour included.
"""
import os, socket, sys, threading, time

STATE = os.environ.get("SIM_STATE", "/tmp/doz-sim")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9603

def power():
    try: return open(f"{STATE}/power").read().strip() == "1"
    except OSError: return False

def booted():
    try: return open(f"{STATE}/booted").read().strip()
    except OSError: return ""

def boot_text():
    boot = open(f"{STATE}/boot").read().strip() if os.path.exists(f"{STATE}/boot") else "default"
    yield "\x1b[2J\x1b[H\x1b[1;37;44m Cisco Systems, Inc.  UCSC-C220-M4S  BIOS C220M4.4.1.2f \x1b[0m\r\n"
    yield "\r\n  Press <F2> Setup, <F6> Boot Menu, <F7> Diagnostics, <F8> CIMC Config, <F12> Network Boot\r\n\r\n"
    yield f"  Boot device override: \x1b[1;33m{boot}\x1b[0m\r\n"
    if boot == "pxe":
        yield "  Intel(R) Boot Agent GE v1.5.88\r\n  CLIENT MAC ADDR: AA BB CC DD FF 01\r\n  DHCP....\x1b[1;31m No offer received\x1b[0m\r\n"
    yield "\r\n[    0.000000] Linux version 6.8.0-45-generic (buildd@lcy02) #45-Ubuntu SMP\r\n"
    yield "[    0.000000] Command line: BOOT_IMAGE=/vmlinuz root=/dev/sda2 console=ttyS0,115200n8\r\n"
    yield "[    1.204113] \x1b[32mOK\x1b[0m  Reached target Multi-User System.\r\n\r\n"
    yield "Ubuntu 24.04 LTS web01 ttyS0\r\n\r\nweb01 login: "

def handle(conn):
    seen_boot, was_on, line = None, False, b""
    conn.settimeout(0.2)
    while True:
        on = power()
        b = booted()
        if on and (not was_on or b != seen_boot):
            print(f"{time.strftime('%T')} boot text ({'power on' if not was_on else 'reset'})",
                  flush=True)
            for chunk in boot_text():
                conn.sendall(chunk.encode()); time.sleep(0.05)
            seen_boot = b
        was_on = on
        try:
            data = conn.recv(1024)
            if not data: return
        except socket.timeout:
            continue
        # Drop telnet negotiation (IAC ...), echo the rest.
        out = bytearray(); i = 0
        while i < len(data):
            if data[i] == 255 and i + 1 < len(data):
                i += 3 if data[i+1] in (251, 252, 253, 254) else 2
                continue
            out.append(data[i]); i += 1
        if not on or not out: continue
        for ch in bytes(out):
            if ch in (13, 10):
                cmd = line.decode(errors="replace").strip(); line = b""
                if cmd == "reboot":
                    # An OS-initiated reboot: the POST screen again, through
                    # the serial line alone (no chassis control involved).
                    print(f"{time.strftime('%T')} boot text (os reboot)", flush=True)
                    conn.sendall(b"\r\n[  OK  ] Reached target Reboot.\r\n")
                    for chunk in boot_text():
                        conn.sendall(chunk.encode()); time.sleep(0.05)
                    continue
                reply = {"uname -a": "Linux web01 6.8.0-45-generic x86_64 GNU/Linux",
                         "hostname": "web01", "whoami": "root"}.get(cmd, f"{cmd}: simulated" if cmd else "")
                conn.sendall(b"\r\n" + (reply.encode() + b"\r\n" if reply else b"") + b"root@web01:~# ")
            elif ch in (8, 127):
                if line: line = line[:-1]; conn.sendall(b"\b \b")
            else:
                line += bytes([ch]); conn.sendall(bytes([ch]))

srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT)); srv.listen(4)
print(f"fake serial on {PORT}", flush=True)
while True:
    c, _ = srv.accept()
    print("SOL backend connected", flush=True)
    threading.Thread(target=handle, args=(c,), daemon=True).start()
