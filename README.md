# CHSnapstart - Tiny Kernel Direct Boot Demo

## 1. 项目简介 / Introduction

本项目致力于展示基于 Cloud Hypervisor 的极速虚拟机启动方案。通过构建高度裁剪的 "Tiny Kernel" (极简内核) 并采用 Direct Kernel Boot 模式，实现了毫秒级的冷启动性能。

核心特点：
*   **Tiny Kernel**: 针对 Cloud Hypervisor + Virtio 场景深度定制的 Linux 内核，剔除了无用模块，体积小，启动快。
*   **Direct Boot**: 绕过 BIOS/UEFI 和 Bootloader，直接加载内核。
*   **No Initrd**: 禁用 Initramfs，内核直接挂载根文件系统 (/dev/vda)，进一步减少 IO 和解压开销。
*   **Micro-Benchmark**: `run_tiny_vm.py` 内置了精确的打点统计，可分析内核启动、网络配置及用户态应用（Python）就绪的各阶段耗时。

## 2. 文件说明 / File Structure

*   **`build_tiny_kernel.py`**:
    *   自动化编译脚本。
    *   负责解压内核源码，应用 "Tiny Config" (仅保留 Virtio, Ext4, Net 等必要驱动)，并编译出 `Image_tiny`。
*   **`run_tiny_vm.py`**:
    *   启动与测试脚本。
    *   负责动态注入 Agent 代码和 Init 脚本到 Rootfs。
    *   配置宿主机 TAP 网络。
    *   拉起 Cloud Hypervisor 并在启动完成后打印详细的耗时报表。
*   **`README.md`**: 本文件。

## 3. 环境要求 / Prerequisites

*   **架构**: ARM64 (脚本默认配置 `ARCH=arm64`)。
*   **操作系统**: Linux (推荐 EulerOS, CentOS 或 Ubuntu)。
*   **权限**: 需要 Root 权限 (用于挂载文件系统和配置网络)。
*   **软件依赖**:
    *   `python3`
    *   `cloud-hypervisor` (需安装在 `/usr/local/bin/cloud-hypervisor` 或自行修改脚本)
    *   编译依赖 (参考: `yum install -y git make gcc bison flex openssl-devel elfutils-libelf-devel bc`)

## 4. 快速开始 / Quick Start

### 步骤 1: 准备必要文件

请确保当前目录下有以下文件（需自行准备）：
1.  **Linux 内核源码**: 例如 `linux-5.15.145.tar.xz`。
    *   *注意*: 修改 `build_tiny_kernel.py` 中的 `LOCAL_SOURCE_PATH` 指向您的源码路径。
2.  **根文件系统**: 命名为 `rootfs_fast.ext4`。

### 步骤 2: 编译极简内核

```bash
sudo python3 build_tiny_kernel.py
```
*   成功后，当前目录会生成 `Image_tiny` (约几MB大小)。

### 步骤 3: 运行极速启动测试

```bash
sudo python3 run_tiny_vm.py
```
*   脚本将启动 VM，等待 Python Agent 就绪，并打印如下性能报表：

```text
🧐 耗时分解 (VM内部视角):
  1. [内核启动] Power On -> Init脚本:  85.20 ms
  2. [Shell配置] Init脚本 -> 网络配完: 12.50 ms
  3. [Python加载] 启动Python -> Ready: 45.10 ms
  ------------------------------------------------
  VM 内部就绪时刻 (Uptime):           142.80 ms
  4. [外部开销] 进程创建/网络握手:     20.50 ms

✅ 总耗时 (Host视角): 163.30 ms
```

## 5. 配置说明 / Configuration

*   **内核配置**: 在 `build_tiny_kernel.py` 中的 `config_tweaks` 变量中定义。
*   **启动参数**: 在 `run_tiny_vm.py` 中的 `CMDLINE` 变量中定义。
*   **网络配置**: 默认使用 Link-Local 地址 (169.254.x.x) 和 TAP 设备。

## 6. 注意事项 / Notes

*   本 Demo 会修改宿主机的网络接口 (创建 TAP 设备) 和防火墙规则 (iptables)，请在测试环境运行。
*   `run_tiny_vm.py` 会通过 loop mount 修改 `rootfs_run.ext4` (从 `rootfs_fast.ext4` 复制而来)，请确保有 sudo 权限。