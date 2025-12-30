#!/usr/bin/env python3
import os
import subprocess
import sys
import multiprocessing
import shutil

# === 配置 ===
# 【关键修改】请将此处修改为您本地 tar.xz 文件的绝对路径或相对路径
LOCAL_SOURCE_PATH = "./linux-5.15.145.tar.xz" 

# 编译工作目录配置
KERNEL_TAR = "linux-tiny.tar.xz" # 脚本内部使用的临时文件名
BUILD_DIR = "linux-tiny-build"

def run(cmd): subprocess.run(cmd, shell=True, check=True)

def main():
    if os.geteuid() != 0: 
        print("请使用 root 运行 (编译内核需要)")
        sys.exit(1)
    
    # 1. 检查并准备源码
    if not os.path.exists(KERNEL_TAR):
        print(f"🔍 检查本地源码: {LOCAL_SOURCE_PATH}")
        if os.path.exists(LOCAL_SOURCE_PATH):
            print(f"📦 复制源码到工作目录...")
            shutil.copy(LOCAL_SOURCE_PATH, KERNEL_TAR)
        else:
            print(f"❌ 错误: 找不到本地文件: {LOCAL_SOURCE_PATH}")
            print("   请修改脚本中的 LOCAL_SOURCE_PATH 变量，或将文件放入当前目录。")
            sys.exit(1)
    
    # 2. 解压
    if not os.path.exists(BUILD_DIR):
        print("📦 解压源码 (这可能需要一分钟)...")
        os.makedirs(BUILD_DIR)
        # --strip-components=1 确保解压内容直接在 BUILD_DIR 下，而不是再套一层目录
        run(f"tar -xf {KERNEL_TAR} -C {BUILD_DIR} --strip-components=1")

    print("⚙️  配置极简内核 (Tiny Config)...")
    os.chdir(BUILD_DIR)
    
    # 清理旧配置
    run("make mrproper")
    
    # 使用 ARM64 默认配置作为基础
    run("make ARCH=arm64 defconfig")
    
    # === 极简配置 (Tiny Config) ===
    # 这是一个针对 Cloud Hypervisor + Virtio 优化的最小集
    config_tweaks = """
# === 必须开启 (Built-in 驱动，抛弃 initramfs) ===
CONFIG_VIRTIO=y
CONFIG_VIRTIO_PCI=y
CONFIG_VIRTIO_MMIO=y
CONFIG_VIRTIO_BLK=y
CONFIG_VIRTIO_NET=y
CONFIG_EXT4_FS=y
CONFIG_NET=y
CONFIG_INET=y
CONFIG_PACKET=y
CONFIG_UNIX=y
CONFIG_SERIAL_AMBA_PL011=y
CONFIG_SERIAL_AMBA_PL011_CONSOLE=y
CONFIG_MAGIC_SYSRQ=y
CONFIG_TMPFS=y
CONFIG_DEVTMPFS=y
CONFIG_DEVTMPFS_MOUNT=y

# === 必须关闭 (剔除冗余，加速启动) ===
# 禁用模块 (全静态编译)
CONFIG_MODULES=n
# 禁用 Initrd (直接挂载磁盘)
CONFIG_BLK_DEV_INITRD=n
# 禁用不必要的子系统
CONFIG_SCSI=n
CONFIG_USB_SUPPORT=n
CONFIG_SOUND=n
CONFIG_DRM=n
CONFIG_FB=n
CONFIG_INPUT_MOUSE=n
CONFIG_INPUT_KEYBOARD=n
# 禁用审计和调试
CONFIG_AUDIT=n
CONFIG_FTRACE=n
CONFIG_KPROBES=n
CONFIG_DEBUG_KERNEL=n
CONFIG_SCHED_DEBUG=n
# 禁用其他文件系统
CONFIG_XFS_FS=n
CONFIG_BTRFS_FS=n
CONFIG_AUTOFS_FS=n
CONFIG_NTFS_FS=n
CONFIG_FUSE_FS=n
# 极简网络
CONFIG_IPV6=n
CONFIG_WLAN=n
CONFIG_WIRELESS=n
CONFIG_BLUETOOTH=n
"""
    with open(".config", "a") as f:
        f.write(config_tweaks)
    
    # 更新配置 (自动接受默认值)
    print("🔄 应用配置...")
    subprocess.run("yes '' | make ARCH=arm64 oldconfig", shell=True)

    # 3. 编译
    cpu_count = multiprocessing.cpu_count()
    print(f"🔨 开始编译 Image (使用 {cpu_count} 核心)...")
    print("   (这可能需要 5-15 分钟，取决于机器性能)")
    
    try:
        run(f"make ARCH=arm64 Image -j{cpu_count}")
    except subprocess.CalledProcessError:
        print("\n❌ 编译失败！")
        print("请检查是否安装了必要的依赖库：")
        print("yum install -y git make gcc bison flex openssl-devel elfutils-libelf-devel bc")
        sys.exit(1)
        
    # 4. 输出产物
    os.chdir("..")
    if os.path.exists("Image_tiny"): os.remove("Image_tiny")
    
    src_image = f"{BUILD_DIR}/arch/arm64/boot/Image"
    if os.path.exists(src_image):
        shutil.copy(src_image, "Image_tiny")
        size_mb = os.path.getsize("Image_tiny") / 1024 / 1024
        print(f"\n✅ 极简内核构建成功: ./Image_tiny")
        print(f"   文件大小: {size_mb:.2f} MB")
        print("   现在您可以运行 run_tiny_vm.py 来测试极速启动了！")
    else:
        print("❌ 错误：编译看似完成，但未找到 arch/arm64/boot/Image")

if __name__ == "__main__":
    main()
