# BakaSU for Sunelan Q968 (Unisoc UMS9157)

把 [BakaSU](https://github.com/Baka-SU/BakaSU)（KernelSU downstream）集成进
**Sunelan Q968 内核**（`5.4.210-android12-9-g96901d45d5a5-ab50`，5.4.210 GKI 骨架
+ 设备 `boot_a.bin` 实现），产出可直接 `fastboot flash boot` 的镜像。

内核源码：[baipinaaa/android_kernel_ums9157](https://github.com/baipinaaa/android_kernel_ums9157)

## 为什么用「手动钩子」模式

BakaSU 有三种钩子模式：

| 模式 | 适用 | 本机能否用 |
|---|---|---|
| Tracepoint Syscall Redirect | 仅 GKI2（内核 5.10+） | ✗ |
| **Manual Hook** | 3.4 – 6.18 | ✓ |
| SUSFS Inline Hook | 4.3+，但要额外的 SUSFS 内核补丁 | 暂未启用 |

本机内核是 5.4.210（非 GKI2），所以走 `CONFIG_KSU_MANUAL_HOOK`。

## 集成做了什么

`scripts/integrate.py` 幂等完成四件事：

1. **拷贝源码**：`BakaSU/kernel/` → `<内核>/drivers/kernelsu/`（107 个文件）
2. **挂载**：`drivers/Kconfig` 加 `source "drivers/kernelsu/Kconfig"`，
   `drivers/Makefile` 加 `obj-$(CONFIG_KSU) += kernelsu/`
3. **手动钩子**（按 [官方文档](https://bakasu.org/zh-Hans/guide/manual-integrate) 的 5.4 分支）：

   | 文件 | 钩子 | 说明 |
   |---|---|---|
   | `fs/stat.c` | `ksu_handle_stat` + `ksu_handle_newfstat_ret` + `ksu_handle_fstat64_ret` | newfstatat / fstatat64 / newfstat / fstat64 |
   | `fs/exec.c` | `ksu_handle_execveat` / `ksu_handle_post_execveat` | 钩在 `__do_execve_file`（5.4 的真实实现，`do_execveat_common` 只是薄封装） |
   | `fs/open.c` | `ksu_handle_faccessat` | `SYSCALL_DEFINE3(faccessat)` |
   | `kernel/reboot.c` | `ksu_handle_sys_reboot` | `SYSCALL_DEFINE4(reboot)` |
   | `drivers/input/input.c` | `ksu_handle_input_handle_event` | `input_event()` |

   `setresuid` / `sys_read` 两个钩子**没有**手改 —— 5.4 走
   `CONFIG_KSU_MANUAL_HOOK_AUTO_{SETUID,INITRC,INPUT}_HOOK` 的 LSM 自动钩子
   （官方文档：只有 6.8+ 才必须手改）。

4. **SELinux 符号导出**（BakaSU 需要跨编译单元访问）：

   | 文件 | 符号 |
   |---|---|
   | `security/selinux/selinuxfs.c` | `write_op[]`、`sel_handle_status_ops` |
   | `security/selinux/ss/services.c` | `context_struct_compute_av`（声明+定义两处）、`security_dump_masked_av` |

5. **defconfig**（`arch/arm64/configs/sprd_ums9157_defconfig`）：

   ```
   CONFIG_KSU=y
   CONFIG_KSU_MANUAL_HOOK=y
   # CONFIG_KSU_TRACEPOINT_HOOK is not set
   CONFIG_KSU_MULTI_MANAGER_SUPPORT=y
   CONFIG_KSU_MANUAL_HOOK_AUTO_SETUID_HOOK=y
   CONFIG_KSU_MANUAL_HOOK_AUTO_INITRC_HOOK=y
   CONFIG_KSU_MANUAL_HOOK_AUTO_INPUT_HOOK=y
   ```

## 编译

GitHub Actions 手动触发（`Actions → bakasu-kernel-build → Run workflow`）：

- 工具链：**Android clang 12.0.5 (r416183b)**，`LLVM=1` + ThinLTO + CFI + SCS
  （与设备原厂内核一致；换工具链会导致 `__cfi_slowpath` 缺失、原厂模块崩）
- 产物：`boot.img`（内嵌 AVB0 + AvbFooter，可直接刷）、`modules.tgz`、`Image`、`System.map`
- 自动发 release：`v1.0.<run_number>`

本地集成（已有内核源码时）：

```bash
git clone https://github.com/Baka-SU/BakaSU
python3 scripts/integrate.py <内核目录> BakaSU/kernel sprd_ums9157_defconfig
```

## 刷入

```bash
adb reboot bootloader          # 不要用 adb reboot fastboot，那是 fastbootd，会写 misc BCB
fastboot flash boot boot.img
fastboot reboot
```

设备已解锁（`verifiedbootstate=orange`）、verity 已关，无需改 `vbmeta`。
需事先执行过 `fastboot --disable-verification flash vbmeta`（本机已完成）。

## 验证

```bash
adb shell "su -c id"                    # uid=0(root)
adb shell "cat /proc/kallsyms | grep -c ksu_"   # > 0
adb shell "lsmod | wc -l"               # 190 左右，原厂模块全在
```

## 已知限制

- **`vendor_boot` 保持原厂**，157 个厂商模块原样加载（靠内核 `check_version()`
  放行 CRC 差异，见内核仓库 README）。
- **SUSFS 未启用**：需要 simonpunk 的 `susfs4ksu` 5.4 补丁（改
  `fs/`、`mm/`、`security/selinux/avc.c` 等），本仓库暂不含。
- **Re:Kernel / Lineage Hide / selinux_hide 补丁未打**：这些是
  [sukisu-oscarc-lto24](https://github.com/baipinaaa/sukisu-oscarc-lto24) 针对
  5.4.302 + ReSukiSU rc3 做的，BakaSU main 的 `selinux_hide.c` 已重构（2145 → 1837 行），
  直接套会 FAIL。需要时按新版本重做。
- 内核侧校验以 CI 为准：本机无 aarch64 交叉编译器。

## 文件结构

```
BakaSU_For_SunelanQ968/
├── .github/workflows/build.yml          # 集成 + 编译 + 打包 + 发 release
├── patches/01_bakasu_manual_hook.patch  # 上述内核改动的完整 diff（参考用）
├── scripts/
│   ├── integrate.py                     # 幂等集成脚本（拷贝 + 钩子 + 配置）
│   ├── pack_boot.py                     # Image + ramdisk -> 带 AVB 的 boot.img
│   └── boot_assets/
│       ├── boot_header_v4.bin           # 设备原厂 boot header 前 4096 B
│       ├── boot_vbmeta.bin              # 原厂内嵌 vbmeta（2304 B）
│       └── ramdisk.cpio.lz4             # 原厂 ramdisk（1,471,146 B）
└── README.md
```
