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

`scripts/integrate.py` 幂等完成五件事：

1. **挂载源码**：`drivers/kernelsu` → 指向 `BakaSU/kernel/` 的**符号链接**
   （`Kbuild:71` 硬检查 `$(KSU_SRC)/../.git` 必须存在，拷贝代码会 `$(error)`；
   且 `git rev-list --count HEAD` 要完整历史，clone 不能 `--depth 1`）
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

## 管理器签名：信任 sukisu-oscarc-lto24 的自签 APK

本仓库**不构建 manager APK**，统一用
[baipinaaa/sukisu-oscarc-lto24](https://github.com/baipinaaa/sukisu-oscarc-lto24)
的 `.github/workflows/build-bakasu-manager.yml` 产物（包名 `com.resukisu.resukisu`）。
那个 APK 用自签 keystore，内核默认只认官方签名，所以要**把对方证书指纹烧进内核**。

### 原理

`bakasu/kernel/manager/apk_sign.c` 从 APK 的 v2 签名块取**第一张证书的 DER**，
比对「DER 字节数 + DER 的 sha256」。与 APK 内容、包名、apksigner 参数组合都无关。

### 两处常量

workflow `env:` 里各一份，注入步骤在「检出 BakaSU」之后：

```
MANAGER_SIGN_SIZE = 0x55d       (证书 DER 字节数 = 1373)
MANAGER_SIGN_HASH = 48480a1f…c909a7d   (证书 DER 的 sha256)
```

这是**纯公开的证书指纹，不含私钥**。私钥只在对方仓库的 GitHub Secrets 里
（`MANAGER_KEYSTORE_BASE64` / `MANAGER_KEYSTORE_PASSWORD`），本仓库不持有、也不该持有 —— 
一旦私钥公开，任何 APK 都能被这个内核认成 manager（manager 权限包括改 allowlist 授权 root）。

`sed` 改的是 `bakasu/kernel/manager/manager_sign.h`：

```c
#define EXPECTED_SIZE_BAKASU 0x55d
#define EXPECTED_HASH_BAKASU "48480a1f…"
```

`apk_sign_keys[]` 里 `{ EXPECTED_SIZE_BAKASU, EXPECTED_HASH_BAKASU }`
**无条件编入且排第一**（不在 `#ifdef CONFIG_KSU_MULTI_MANAGER_SUPPORT` 里）。

### 13.1 坑一：`CERT_MAX_LENGTH` 1024 硬上限

`bakasu/kernel/manager/apk_sign.c:137` 有一条上游硬编码：

```c
#define CERT_MAX_LENGTH 1024
    if (certificate_size > CERT_MAX_LENGTH) {
        pr_info("cert length overlimit\n");
        return false;
    }
```

这个检查在算 sha256 **之前**就 return。对方密钥是 **RSA 4096，证书 DER = 1373 字节**，
超出 349 字节 —— 指纹注得再对也走不到比对。

**判别法**：`dmesg` 里正常的 app 会先打 `KernelSU: sha256: <64hex>`；
被上限拒掉的**没有** `sha256` 行，只有 `KernelSU: cert length overlimit`。

修复（同一条注入步骤里）：

```bash
sed -i "s|^#define CERT_MAX_LENGTH .*|#define CERT_MAX_LENGTH 2048|" bakasu/kernel/manager/apk_sign.c
```

`char cert[CERT_MAX_LENGTH]` 放在 kworker 的 16 KB 内核栈上，2048 安全；
主流证书（RSA 2048/3072、ECDSA P-256/P-384）本来就 ≤ 1024，不受影响。

工作流里有一条联动断言：

```bash
CAP=$(grep -m1 '^#define CERT_MAX_LENGTH ' "$F" | awk '{print $3}')
[ $((CAP)) -ge $((MANAGER_SIGN_SIZE)) ] || exit 1
```

**以后换密钥必做**：先 `keytool -exportcert -alias <alias> -keystore <jks> -file c.der`
然后 `stat -c%s c.der && sha256sum c.der`，把两个值填进 `env:`；
若 DER > 1024，同时确认 `CERT_MAX_LENGTH` 够大。

### 13.2 坑二：BakaSU ref 必须与 manager 的 UAPI 对齐

manager 在 BakaSU main `548d1dda060ca198823d8808804d01f5209ef4a9`（UAPI v6）上构建，
本仓库 `bakasu_ref` 默认值也是它。若内核停在 `v4.2.0-rc3`（UAPI v4），
会出现 manager 功能置灰、`libksud.so` 的 supercall 号段对不上。

### 13.3 坑三：换签名不能覆盖安装

从官方 APK 切过来要 `adb uninstall com.resukisu.resukisu`；
`/data/adb/ksu/` 下的配置和模块不受影响。

若内核换了指纹，必须**重刷内核**（`EXPECTED_HASH_BAKASU` 是编译期常量），没有运行时办法。

### 验证

```bash
adb shell su -c 'dmesg | grep -i "is_manager"'          # 必须是 is_manager: 1
adb shell su -c '/data/adb/ksud debug get-sign $(pm path com.resukisu.resukisu | sed "s/package://")'
# 期望: size: 0x55d, hash: 48480a1f…
```

若 `debug get-sign` 输出 `size: 0x377, hash: d3469712…`，说明设备上装的还是
**官方** manager APK，不是对方仓库的产物。

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
