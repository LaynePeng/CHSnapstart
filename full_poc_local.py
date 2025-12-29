#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import shutil
import socket
import json
import urllib.request
import urllib.error

# ==================== 配置区域 ====================
ARCH = "aarch64" if os.uname().machine == "aarch64" else "x86_64"
# ARM64 使用 ttyAMA0，且必须开启 PCI
CMDLINE_BASE = "console=ttyAMA0 reboot=k panic=1 rootfstype=virtiofs root=myfs rw init=/vm_init.sh"

BASE_DIR = os.getcwd()
WORK_DIR = os.path.join(BASE_DIR, "poc_workspace")
ROOTFS_MOUNT = os.path.join(WORK_DIR, "rootfs_mount") 
SNAPSHOT_DIR = os.path.join(WORK_DIR, "snapshot_store")

LOCAL_KERNEL = "vmlinux"
LOCAL_ROOTFS = "rootfs.ext4"

HOST_IP = "172.16.0.1"
VM_IP = "172.16.0.2"
AGENT_PORT = 8000

CH_BIN = "cloud-hypervisor"
VIRTIOFSD_BIN = None
possible_fsd = ["virtiofsd", "qemu-virtiofsd", "/usr/libexec/virtiofsd", "/usr/local/bin/virtiofsd"]
for p in possible_fsd:
    if shutil.which(p): 
        VIRTIOFSD_BIN = p
        break

SNAPSHOT_STRATEGIES = [
    ("Standard", "boot=1"),
    ("No-PMU", "boot=1,pmu=off"),
    ("Aggressive", "boot=1,pmu=off,sve=off")
]
# =================================================

def log(msg): print(f"\033[92m[POC] {msg}\033[0m")
def warn(msg): print(f"\033[93m[WARN] {msg}\033[0m")
def error(msg): print(f"\033[91m[ERROR] {msg}\033[0m"); sys.exit(1)

def run(cmd_list, bg=False, check=True):
    if bg:
        # 后台运行，静默输出
        return subprocess.Popen(cmd_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # 前台运行，捕获输出
        ret = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check and ret.returncode != 0:
            return False, ret.stdout.decode()
        return True, ret.stdout.decode()

def safe_kill(proc):
    """安全地终止进程并回收资源"""
    if proc and proc.poll() is None:
        proc.kill()
        proc.wait() # 防止僵尸进程

def run_curl_api(sock, method, path, body=None):
    cmd = ["curl", "-s", "--unix-socket", sock, "-X", method, f"http://localhost{path}"]
    if body:
        cmd += ["-d", json.dumps(body)]
    
    code_cmd = cmd + ["-w", "%{http_code}", "-o", "/dev/null"]
    ret = subprocess.run(code_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    http_code = ret.stdout.decode().strip()
    
    if http_code not in ['200', '204']:
        ret_body = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return False, f"HTTP {http_code}: {ret_body.stdout.decode()}"
    
    return True, "OK"

def prepare_resources():
    log(f"准备环境: {WORK_DIR}")
    
    kernel_src = os.path.join(BASE_DIR, LOCAL_KERNEL)
    rootfs_src = os.path.join(BASE_DIR, LOCAL_ROOTFS)

    if not os.path.exists(kernel_src): error(f"未找到内核: {LOCAL_KERNEL}")
    if not os.path.exists(rootfs_src): error(f"未找到镜像: {LOCAL_ROOTFS}")

    if os.path.exists(WORK_DIR):
        subprocess.run(f"umount {ROOTFS_MOUNT}", shell=True, stderr=subprocess.DEVNULL)
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR)
    os.makedirs(ROOTFS_MOUNT)
    os.makedirs(SNAPSHOT_DIR)

    log(f"复制本地资源...")
    shutil.copy(kernel_src, f"{WORK_DIR}/vmlinux")
    shutil.copy(rootfs_src, f"{WORK_DIR}/rootfs.ext4")
    
    log("挂载 Rootfs (模拟容器解压层)...")
    ok, msg = run(["mount", "-o", "loop", f"{WORK_DIR}/rootfs.ext4", ROOTFS_MOUNT]) # type: ignore
    if not ok: error(f"挂载失败: {msg}")

def inject_agent_code():
    log("注入 Python Agent 代码...")
    agent_code = """
import http.server
import socketserver
import json
import sys
import time

PORT = 8000

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get('Content-Length', 0))
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        resp = {
            "status": "Ready", 
            "info": "Python Agent", 
            "runtime": sys.version.split()[0]
        }
        self.wfile.write(json.dumps(resp).encode('utf-8'))

print(f"Agent starting on port {PORT}...")
httpd = socketserver.TCPServer(("", PORT), Handler)
httpd.serve_forever()
"""
    with open(f"{ROOTFS_MOUNT}/agent.py", "w") as f: f.write(agent_code)

    init_script = f"""#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs dev /dev
ip addr add {VM_IP}/24 dev eth0
ip link set up dev eth0
ip route add default via {HOST_IP}
exec python3 /agent.py
"""
    with open(f"{ROOTFS_MOUNT}/vm_init.sh", "w") as f: f.write(init_script)
    os.chmod(f"{ROOTFS_MOUNT}/vm_init.sh", 0o755)

# ================= 核心：快照策略尝试 =================
def attempt_make_snapshot():
    log("\n>>> [阶段1] 尝试制作 SnapStart 快照 <<<")
    
    fs_sock = f"{WORK_DIR}/fs_snap.sock"
    if not VIRTIOFSD_BIN: error("未找到 virtiofsd，请先安装。")
    
    fs_cmd = [
        VIRTIOFSD_BIN, 
        f"--socket-path={fs_sock}", 
        f"--shared-dir={ROOTFS_MOUNT}", 
        "--cache=always", 
        "--sandbox=chroot"
    ]
    fs_proc = run(fs_cmd, bg=True)
    time.sleep(1)

    success_strategy = None

    for name, cpu_param in SNAPSHOT_STRATEGIES:
        log(f"--- 尝试策略: {name} (CPU: {cpu_param}) ---")
        ch_sock = f"{WORK_DIR}/ch_snap.sock"
        if os.path.exists(ch_sock): os.remove(ch_sock)
        
        # 【修复】初始化为 None，防止 run() 报错后 ch_proc 未定义
        ch_proc = None 
        
        try:
            ch_cmd = [
                CH_BIN, f"--api-socket={ch_sock}",
                f"--kernel={WORK_DIR}/vmlinux",
                f"--cpus={cpu_param}",
                f"--memory=size=512M",
                f"--fs=tag=myfs,socket={fs_sock},num_queues=1,queue_size=1024",
                f"--net=tap=tap_snap,mac=AA:FC:00:00:00:99",
                f"--console=off", f"--serial=file={WORK_DIR}/snap_serial.log",
                f"--cmdline={CMDLINE_BASE}"
            ]
            
            run(["ip", "tuntap", "add", "dev", "tap_snap", "mode", "tap"], check=False)
            run(["ip", "addr", "add", f"{HOST_IP}/24", "dev", "tap_snap"], check=False)
            run(["ip", "link", "set", "up", "tap_snap"], check=False)

            ch_proc = run(ch_cmd, bg=True)
            
            agent_ready = False
            for _ in range(50): # 5s 超时
                try:
                    s = socket.socket(); s.settimeout(0.1)
                    s.connect((VM_IP, AGENT_PORT)); s.close()
                    agent_ready = True; break
                except: time.sleep(0.1)
            
            if not agent_ready:
                warn(f"Agent 启动超时 (策略: {name})")
                raise Exception("Agent Timeout")

            ok, msg = run_curl_api(ch_sock, "PUT", "/api/v1/vm.pause")
            if not ok: raise Exception(f"Pause Failed: {msg}")

            snap_url = f"file://{SNAPSHOT_DIR}"
            ok, msg = run_curl_api(ch_sock, "PUT", "/api/v1/vm.snapshot", {"destination_url": snap_url})
            
            if ok:
                log(f"\033[92m>>> 策略 [{name}] 成功! <<<\033[0m")
                success_strategy = (name, cpu_param)
                safe_kill(ch_proc) # 成功后清理
                break 
            else:
                warn(f"策略 [{name}] 失败: {msg}")

        except Exception as e:
            # warn(f"策略 [{name}] 异常: {e}")
            pass
        finally:
            # 【修复】无论成功失败，确保清理当前轮次的进程
            safe_kill(ch_proc)
            run(["ip", "link", "del", "tap_snap"], check=False)
            time.sleep(0.5)
    
    safe_kill(fs_proc)

    if success_strategy:
        return "RESTORE", success_strategy[1]
    else:
        warn("\033[93m所有快照策略均失败。将自动降级为 [Direct Boot] 模式。\033[0m")
        return "BOOT", "boot=1"

# ================= 性能压测 =================
def benchmark_performance(mode, cpu_param):
    log(f"\n>>> [阶段2] 开始性能压测 (模式: {mode}) <<<")
    
    run_id = "bench"
    run(["ip", "tuntap", "add", "dev", f"tap{run_id}", "mode", "tap"], check=False)
    run(["ip", "addr", "add", f"{HOST_IP}/24", "dev", f"tap{run_id}"], check=False)
    run(["ip", "link", "set", "up", f"tap{run_id}"], check=False)

    fs_sock = f"{WORK_DIR}/fs_bench.sock"
    fs_cmd = [
        VIRTIOFSD_BIN, 
        f"--socket-path={fs_sock}", 
        f"--shared-dir={ROOTFS_MOUNT}", 
        "--cache=always", 
        "--sandbox=chroot"
    ]
    fs_proc = run(fs_cmd, bg=True)
    time.sleep(0.5)

    ch_sock = f"{WORK_DIR}/ch_bench.sock"
    if os.path.exists(ch_sock): os.remove(ch_sock)
    
    start_ts = time.time()
    
    if mode == "RESTORE":
        ch_cmd = [
            CH_BIN, f"--api-socket={ch_sock}",
            f"--restore={SNAPSHOT_DIR}",
            f"--cpus={cpu_param}", 
            f"--fs=tag=myfs,socket={fs_sock},num_queues=1,queue_size=1024",
            f"--net=tap=tap{run_id},mac=AA:FC:00:00:00:01",
            f"--console=off", f"--serial=file=/dev/null"
        ]
    else:
        ch_cmd = [
            CH_BIN, f"--api-socket={ch_sock}",
            f"--kernel={WORK_DIR}/vmlinux",
            f"--cpus={cpu_param}",
            f"--memory=size=512M",
            f"--fs=tag=myfs,socket={fs_sock},num_queues=1,queue_size=1024",
            f"--net=tap=tap{run_id},mac=AA:FC:00:00:00:01",
            f"--console=off", f"--serial=file=/dev/null",
            f"--cmdline={CMDLINE_BASE}"
        ]

    ch_proc = run(ch_cmd, bg=True)

    ready = False
    for _ in range(100):
        try:
            s = socket.socket(); s.settimeout(0.05)
            s.connect((VM_IP, AGENT_PORT)); s.close()
            ready = True; break
        except: time.sleep(0.05)
    
    ready_ts = time.time()
    
    if not ready:
        safe_kill(ch_proc)
        safe_kill(fs_proc)
        run(["ip", "link", "del", f"tap{run_id}"], check=False)
        error("压测启动超时！")

    startup_time = (ready_ts - start_ts) * 1000
    log(f"Agent 就绪! 总启动耗时: {startup_time:.2f} ms")

    try:
        req_start = time.time()
        req = urllib.request.Request(f"http://{VM_IP}:{AGENT_PORT}", data=b'{}', method="POST")
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req) as f:
            pass
        print(f"请求响应耗时: {(time.time()-req_start)*1000:.2f} ms")
    except Exception as e:
        warn(f"请求失败: {e}")

    safe_kill(ch_proc)
    safe_kill(fs_proc)
    run(["ip", "link", "del", f"tap{run_id}"], check=False)

def cleanup():
    log("清理环境...")
    subprocess.run(f"umount {ROOTFS_MOUNT}", shell=True, stderr=subprocess.DEVNULL)
    # subprocess.run(f"rm -rf {WORK_DIR}", shell=True) 

def main():
    if os.geteuid() != 0: error("请使用 sudo 运行")
    if not shutil.which(CH_BIN): error(f"找不到 {CH_BIN}")
    if not VIRTIOFSD_BIN: error("找不到 virtiofsd")

    try:
        prepare_resources()
        inject_agent_code()
        
        mode, cpu_param = attempt_make_snapshot()
        
        print("\n" + "="*40)
        print(f"最终决策模式: [{mode}]")
        print(f"最佳 CPU 参数: [{cpu_param}]")
        print("="*40 + "\n")
        
        log(">>> 第一次运行 (Cold Run)...")
        benchmark_performance(mode, cpu_param)
        
        log("\n>>> 第二次运行 (Warm Run)...")
        benchmark_performance(mode, cpu_param)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        cleanup()

if __name__ == "__main__":
    main()