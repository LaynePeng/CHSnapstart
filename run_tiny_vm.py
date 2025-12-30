#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import socket
import shutil

# === 配置 ===
# 核心路径
CH_BIN = "/usr/local/bin/cloud-hypervisor"
KERNEL = "./Image_tiny"            # 刚刚编译的极简内核
IMAGE_SRC = "./rootfs_fast.ext4"   # V2 Rootfs
IMAGE_RUN = "./rootfs_run.ext4"

# 极简启动参数 (无 initrd，直接挂载 /dev/vda)
# quiet: 减少打印加速
# console=ttyAMA0: 必须保留，用于传回打点数据
CMDLINE = "root=/dev/vda rw console=ttyAMA0 quiet mitigations=off"
CPUS_CFG = "boot=1"

# 避难所网络 (Link-Local)
HOST_IP = "169.254.10.1"
HOST_MAC = "aa:bb:cc:dd:ee:01"
VM_IP = "169.254.10.2"
VM_MAC = "aa:bb:cc:dd:ee:02"
TAP_DEV = "tap_tiny"
AGENT_PORT = 8000
LOG_FILE = "./vm_tiny.log"

def run(cmd): subprocess.run(cmd, shell=True)

def setup_network():
    # 1. 创建 TAP
    run(f"ip link del {TAP_DEV} 2>/dev/null")
    run(f"ip tuntap add dev {TAP_DEV} mode tap")
    run(f"ip link set dev {TAP_DEV} address {HOST_MAC}") 
    run(f"ip addr add {HOST_IP}/16 dev {TAP_DEV}")
    run(f"ip link set up {TAP_DEV}")
    
    # 2. 静态 ARP (双向锁死)
    run(f"ip neigh add {VM_IP} lladdr {VM_MAC} dev {TAP_DEV}")
    
    # 3. 关闭 Offload
    run(f"ethtool -K {TAP_DEV} tx off rx off >/dev/null 2>&1")
    
    # 4. 暴力放行
    run("setenforce 0 2>/dev/null")
    run("systemctl stop firewalld 2>/dev/null")
    run("iptables -F")
    run(f"iptables -I INPUT -i {TAP_DEV} -j ACCEPT")

def main():
    if os.geteuid() != 0: sys.exit("Need Root")
    if not os.path.exists(KERNEL): sys.exit(f"❌ 找不到内核 {KERNEL}，请先运行 build_tiny_kernel.py")

    # 1. 清理
    run(f"killall -9 cloud-hypervisor 2>/dev/null")
    if os.path.exists(LOG_FILE): os.remove(LOG_FILE)

    print("🚀 准备 Tiny Kernel 环境...")
    shutil.copy(IMAGE_SRC, IMAGE_RUN)
    
    # 2. 注入 Agent 和 埋点 Init
    mnt = "mnt_tiny"
    os.makedirs(mnt, exist_ok=True)
    run(f"mount -o loop {IMAGE_RUN} {mnt}")
    try:
        # Agent: 启动后立即打点
        with open(f"{mnt}/agent.py", "w") as f:
            f.write("""
import socket
# 打点：Python Ready
with open('/proc/uptime', 'r') as f:
    up = f.read().split()[0]
with open('/dev/ttyAMA0', 'w') as f:
    f.write(f"MARK:PYTHON_READY:{up}\\n")

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', 8000))
s.listen(1)
while True:
    try:
        c, a = s.accept()
        c.close()
    except: pass
""")
        # Init: 负责挂载和网络
        with open(f"{mnt}/sbin/init", "w") as f:
            f.write(f"""#!/bin/sh
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
mount -t proc proc /proc; mount -t sysfs sysfs /sys

# 打点：内核启动完成 (进入 Init 的第一刻)
read UP < /proc/uptime
echo "MARK:KERNEL_DONE:$UP" > /dev/ttyAMA0

# 配置网络
ip addr add {VM_IP}/16 dev eth0
ip link set eth0 address {VM_MAC}
ip link set eth0 up
ip neigh add {HOST_IP} lladdr {HOST_MAC} dev eth0

# 打点：网络配置完成
read UP < /proc/uptime
echo "MARK:NET_DONE:$UP" > /dev/ttyAMA0

# 启动 Agent
python3 /agent.py &
while true; do sleep 3600; done
""")
        os.chmod(f"{mnt}/sbin/init", 0o755)
    finally:
        run(f"umount {mnt}")
        os.rmdir(mnt)

    setup_network()

    print(f"🔥 启动测试 (Direct Kernel Boot)...")
    cmd = [
        CH_BIN,
        "--kernel", KERNEL,
        # 注意：没有 --initramfs
        "--disk", f"path={IMAGE_RUN}",
        "--cpus", CPUS_CFG,
        "--memory", "size=256M", # Tiny Kernel 内存占用很小
        "--net", f"tap={TAP_DEV},mac={VM_MAC}",
        "--cmdline", CMDLINE,
        "--console", "off",
        "--serial", f"file={LOG_FILE}" # 记录日志用于分析
    ]
    
    start_time = time.time()
    # 启动
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # 探测连接
    connected = False
    for i in range(1000): 
        if proc.poll() is not None: break
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.005) 
            s.connect((VM_IP, AGENT_PORT))
            s.close()
            connected = True
            break
        except:
            pass
        time.sleep(0.001) # 1ms 轮询
            
    end_time = time.time()
    
    if not connected:
        print("\n❌ 启动超时")
        proc.kill()
        run(f"ip link del {TAP_DEV} 2>/dev/null")
        os.system(f"tail -n 20 {LOG_FILE}")
        sys.exit(1)

    # === 计算与打印 ===
    total_time_ms = (end_time - start_time) * 1000
    
    # 解析日志
    t_kernel = 0.0
    t_net = 0.0
    t_python = 0.0
    
    try:
        with open(LOG_FILE, 'r', errors='ignore') as f:
            for line in f:
                if "MARK:" in line:
                    try:
                        parts = line.strip().split(':')
                        tag = parts[1]
                        ts = float(parts[2].strip().split()[0]) * 1000
                        if tag == "KERNEL_DONE": t_kernel = ts
                        if tag == "NET_DONE": t_net = ts
                        if tag == "PYTHON_READY": t_python = ts
                    except: continue
    except: pass

    # 停止进程
    proc.kill()
    run(f"ip link del {TAP_DEV} 2>/dev/null")

    # 打印报表
    print("\n🧐 \033[1m耗时分解 (VM内部视角):\033[0m")
    if t_kernel > 0 and t_python > 0:
        p1 = t_kernel
        p2 = t_net - t_kernel
        p3 = t_python - t_net
        
        # 计算外部 Overhead (Host视角总时间 - VM内部Ready时间)
        overhead = total_time_ms - t_python
        
        print(f"  1. [内核启动] Power On -> Init脚本:  {p1:.2f} ms")
        print(f"  2. [Shell配置] Init脚本 -> 网络配完: {p2:.2f} ms")
        print(f"  3. [Python加载] 启动Python -> Ready: {p3:.2f} ms")
        print(f"  ------------------------------------------------")
        print(f"  VM 内部就绪时刻 (Uptime):           \033[92m{t_python:.2f} ms\033[0m")
        print(f"  4. [外部开销] 进程创建/网络握手:     {overhead:.2f} ms")
        print(f"\n✅ \033[93m总耗时 (Host视角): {total_time_ms:.2f} ms\033[0m")
    else:
        print("⚠️  无法解析日志中的打点数据，可能启动过快导致日志缓冲未刷盘。")
        print(f"Host视角总耗时: {total_time_ms:.2f} ms")

if __name__ == "__main__":
    main()
