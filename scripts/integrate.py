#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BakaSU 内核侧集成脚本（幂等）。

用法:
    python _stock/bakasu/integrate.py <kernel_tree> <bakasu_kernel_dir>

做四件事:
  1. 拷贝 BakaSU kernel/ -> <tree>/drivers/kernelsu/
  2. drivers/Kconfig + drivers/Makefile 挂载
  3. 按 bakasu.org/zh-Hans/guide/manual-integrate 打手动钩子（5.4 适用版本）
     - fs/stat.c           stat hook            (必加)
     - fs/exec.c           execve hook          (必加)
     - fs/open.c           faccessat hook       (必加)
     - kernel/reboot.c     sys_reboot hook      (必加)
     - drivers/input/input.c input hook         (按需必加)
     - security/selinux    4 个符号去 static    (必加)
  4. defconfig 追加 CONFIG_KSU

setuid / sys_read 两个钩子本脚本不加 —— 5.4 走 KSU_MANUAL_HOOK_AUTO_* 的
LSM 自动钩子（Kconfig 里 default y，仅 6.8+ 才必须手改）。
"""
import os
import shutil
import sys

MARK_DRV_KCONFIG = 'source "drivers/kernelsu/Kconfig"'
MARK_DRV_MAKEFILE = 'obj-$(CONFIG_KSU) += kernelsu/'


def read(p):
    with open(p, 'r', encoding='utf-8', errors='surrogateescape') as f:
        return f.read()


def write(p, s):
    with open(p, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
        f.write(s)


def sub_once(text, old, new, tag):
    """替换一次；old 已不在而 new 在则跳过（幂等）；否则必须唯一命中。"""
    if old not in text:
        if new in text:
            print('  [skip] %s (already patched)' % tag)
            return text
        raise SystemExit('  [FAIL] %s: anchor not found' % tag)
    n = text.count(old)
    if n != 1:
        raise SystemExit('  [FAIL] %s: anchor found %d times' % (tag, n))
    print('  [ok]   %s' % tag)
    return text.replace(old, new, 1)


def copy_source(tree, src):
    dst = os.path.join(tree, 'drivers', 'kernelsu')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        '.vscode', '*.o', '*.ko', '*.cmd', '.tmp_*', 'build-*'))
    # 只用 Kbuild 参与 kbuild，剔掉宿主工具
    for junk in ('Makefile', 'setup.sh', 'build-all.sh', 'build-all-x64.sh',
                 'tools', 'preinit'):
        p = os.path.join(dst, junk)
        if os.path.isdir(p):
            shutil.rmtree(p)
            print('  [rm]   drivers/kernelsu/%s/' % junk)
        elif os.path.exists(p):
            os.remove(p)
            print('  [rm]   drivers/kernelsu/%s' % junk)
    n = sum(len(fs) for _, _, fs in os.walk(dst))
    print('  [ok]   drivers/kernelsu copied (%d files)' % n)


def patch_kconfig(tree):
    p = os.path.join(tree, 'drivers', 'Kconfig')
    t = read(p)
    if MARK_DRV_KCONFIG in t:
        print('  [skip] drivers/Kconfig')
        return
    # 在最后一个 endmenu 前插入
    idx = t.rfind('\nendmenu')
    if idx < 0:
        raise SystemExit('  [FAIL] drivers/Kconfig: endmenu not found')
    t = t[:idx + 1] + MARK_DRV_KCONFIG + '\n' + t[idx + 1:]
    write(p, t)
    print('  [ok]   drivers/Kconfig')


def patch_makefile(tree):
    p = os.path.join(tree, 'drivers', 'Makefile')
    t = read(p)
    if MARK_DRV_MAKEFILE in t:
        print('  [skip] drivers/Makefile')
        return
    if not t.endswith('\n'):
        t += '\n'
    t += '\n' + MARK_DRV_MAKEFILE + '\n'
    write(p, t)
    print('  [ok]   drivers/Makefile')


# --------------------------------------------------------------------------
# manual hooks
# --------------------------------------------------------------------------

DECL_STAT = '''#ifdef CONFIG_KSU_MANUAL_HOOK
__attribute__((hot))
extern int ksu_handle_stat(int *dfd, const char __user **filename_user,
			   int *flags);
extern void ksu_handle_newfstat_ret(unsigned int *fd, struct stat __user **statbuf_ptr);
#if defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_COMPAT_STAT64)
extern void ksu_handle_fstat64_ret(unsigned long *fd, struct stat64 __user **statbuf_ptr);
#endif
#endif

'''


def hook_stat(tree):
    p = os.path.join(tree, 'fs', 'stat.c')
    t = read(p)
    if 'ksu_handle_stat' in t:
        print('  [skip] fs/stat.c')
        return
    # 1) 声明块：放在 newfstatat 之前
    t = sub_once(
        t,
        '#if !defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_SYS_NEWFSTATAT)\nSYSCALL_DEFINE4(newfstatat,',
        DECL_STAT + '#if !defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_SYS_NEWFSTATAT)\nSYSCALL_DEFINE4(newfstatat,',
        'fs/stat.c decl')

    # 2) newfstatat / fstatat64：在 vfs_fstatat 调用前 hook
    old = '''SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,
		struct stat __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

	error = vfs_fstatat(dfd, filename, &stat, flag);'''
    new = '''SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,
		struct stat __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_stat(&dfd, &filename, &flag);
#endif
	error = vfs_fstatat(dfd, filename, &stat, flag);'''
    t = sub_once(t, old, new, 'fs/stat.c newfstatat')

    old = '''SYSCALL_DEFINE4(fstatat64, int, dfd, const char __user *, filename,
		struct stat64 __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

	error = vfs_fstatat(dfd, filename, &stat, flag);'''
    new = '''SYSCALL_DEFINE4(fstatat64, int, dfd, const char __user *, filename,
		struct stat64 __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

#ifdef CONFIG_KSU_MANUAL_HOOK /* 32-bit su */
	ksu_handle_stat(&dfd, &filename, &flag);
#endif
	error = vfs_fstatat(dfd, filename, &stat, flag);'''
    t = sub_once(t, old, new, 'fs/stat.c fstatat64')

    # 3) newfstat：返回值 hook
    old = '''SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat(&stat, statbuf);

	return error;'''
    new = '''SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat(&stat, statbuf);

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_newfstat_ret(&fd, &statbuf);
#endif
	return error;'''
    t = sub_once(t, old, new, 'fs/stat.c newfstat')

    # 4) fstat64
    old = '''SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat64(&stat, statbuf);

	return error;'''
    new = '''SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat64(&stat, statbuf);

#ifdef CONFIG_KSU_MANUAL_HOOK /* for 32-bit */
	ksu_handle_fstat64_ret(&fd, &statbuf);
#endif
	return error;'''
    t = sub_once(t, old, new, 'fs/stat.c fstat64')
    write(p, t)


DECL_EXEC = '''#ifdef CONFIG_KSU_MANUAL_HOOK
__attribute__((hot))
extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr,
			       void *argv, void *envp, int *flags);
__attribute__((hot))
extern int ksu_handle_post_execveat(int *fd, struct filename **filename_ptr,
				    void *argv, void *envp, int *flags, int *retval);
#endif

'''


def hook_exec(tree):
    p = os.path.join(tree, 'fs', 'exec.c')
    t = read(p)
    if 'ksu_handle_execveat' in t:
        print('  [skip] fs/exec.c')
        return
    t = sub_once(
        t,
        'static int __do_execve_file(int fd, struct filename *filename,',
        DECL_EXEC + 'static int __do_execve_file(int fd, struct filename *filename,',
        'fs/exec.c decl')
    old = '''{
	char *pathbuf = NULL;
	struct linux_binprm *bprm;
	struct files_struct *displaced;
	int retval;

	if (IS_ERR(filename))'''
    new = '''{
	char *pathbuf = NULL;
	struct linux_binprm *bprm;
	struct files_struct *displaced;
	int retval;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);
#endif
	if (IS_ERR(filename))'''
    t = sub_once(t, old, new, 'fs/exec.c pre-hook')
    old = '''out_ret:
	if (filename)
		putname(filename);
	return retval;
}'''
    new = '''out_ret:
#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_post_execveat(&fd, &filename, &argv, &envp, &flags, &retval);
#endif
	if (filename)
		putname(filename);
	return retval;
}'''
    t = sub_once(t, old, new, 'fs/exec.c post-hook')
    write(p, t)


def hook_faccessat(tree):
    p = os.path.join(tree, 'fs', 'open.c')
    t = read(p)
    if 'ksu_handle_faccessat' in t:
        print('  [skip] fs/open.c')
        return
    old = '''SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)
{
	return do_faccessat(dfd, filename, mode);
}'''
    new = '''#ifdef CONFIG_KSU_MANUAL_HOOK
__attribute__((hot))
extern int ksu_handle_faccessat(int *dfd, const char __user **filename_user,
				int *mode, int *flags);
#endif

SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)
{
#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_faccessat(&dfd, &filename, &mode, NULL);
#endif
	return do_faccessat(dfd, filename, mode);
}'''
    t = sub_once(t, old, new, 'fs/open.c faccessat')
    write(p, t)


def hook_reboot(tree):
    p = os.path.join(tree, 'kernel', 'reboot.c')
    t = read(p)
    if 'ksu_handle_sys_reboot' in t:
        print('  [skip] kernel/reboot.c')
        return
    old = '''SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,
		void __user *, arg)'''
    new = '''#ifdef CONFIG_KSU_MANUAL_HOOK
extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd,
				 void __user **arg);
#endif

SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,
		void __user *, arg)'''
    t = sub_once(t, old, new, 'kernel/reboot.c decl')
    # 函数体：在变量声明之后
    old = '''	char buffer[256];
	int ret = 0;

	/* We only trust the superuser with rebooting the system. */'''
    new = '''	char buffer[256];
	int ret = 0;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_sys_reboot(magic1, magic2, cmd, &arg);
#endif
	/* We only trust the superuser with rebooting the system. */'''
    t = sub_once(t, old, new, 'kernel/reboot.c hook')
    write(p, t)


def hook_input(tree):
    p = os.path.join(tree, 'drivers', 'input', 'input.c')
    t = read(p)
    if 'ksu_handle_input_handle_event' in t:
        print('  [skip] drivers/input/input.c')
        return
    old = '''void input_event(struct input_dev *dev,
		 unsigned int type, unsigned int code, int value)
{
	unsigned long flags;
'''
    new = '''#ifdef CONFIG_KSU_MANUAL_HOOK
extern bool ksu_input_hook __read_mostly;
extern __attribute__((cold)) int ksu_handle_input_handle_event(
			unsigned int *type, unsigned int *code, int *value);
#endif

void input_event(struct input_dev *dev,
		 unsigned int type, unsigned int code, int value)
{
	unsigned long flags;

#ifdef CONFIG_KSU_MANUAL_HOOK
	if (unlikely(ksu_input_hook))
		ksu_handle_input_handle_event(&type, &code, &value);
#endif
'''
    t = sub_once(t, old, new, 'drivers/input/input.c input_event')
    write(p, t)


def export_selinux(tree):
    """4 个 SELinux 静态符号去 static（BakaSU 需要外部可见）。"""
    # selinuxfs.c: write_op / sel_handle_status_ops
    p = os.path.join(tree, 'security', 'selinux', 'selinuxfs.c')
    t = read(p)
    t = sub_once(t,
                 'static ssize_t (*const write_op[])(struct file *, char *, size_t) = {',
                 'ssize_t (*const write_op[])(struct file *, char *, size_t) = {',
                 'selinuxfs.c write_op')
    t = sub_once(t,
                 'static const struct file_operations sel_handle_status_ops = {',
                 'const struct file_operations sel_handle_status_ops = {',
                 'selinuxfs.c sel_handle_status_ops')
    write(p, t)

    # services.c: context_struct_compute_av / security_dump_masked_av
    p = os.path.join(tree, 'security', 'selinux', 'ss', 'services.c')
    t = read(p)
    # context_struct_compute_av 在 services.c 出现两次：前向声明(94) + 定义(613)
    # 两处都必须去 static，否则 "static declaration follows non-static"
    old = 'static void context_struct_compute_av(struct policydb *policydb,'
    new = 'void context_struct_compute_av(struct policydb *policydb,'
    if old in t:
        print('  [ok]   services.c compute_av x%d' % t.count(old))
        t = t.replace(old, new)
    else:
        print('  [skip] services.c compute_av')
    t = sub_once(t,
                 'static void security_dump_masked_av(struct policydb *policydb,',
                 'void security_dump_masked_av(struct policydb *policydb,',
                 'services.c dump_masked_av')
    write(p, t)


CONFIGS = '''
# BakaSU (KernelSU downstream) - manual hook mode for non-GKI2 5.4 kernel
CONFIG_KSU=y
CONFIG_KSU_DEBUG=n
CONFIG_KSU_MANUAL_HOOK=y
# CONFIG_KSU_TRACEPOINT_HOOK is not set
CONFIG_KSU_MULTI_MANAGER_SUPPORT=y
CONFIG_KSU_MANUAL_HOOK_AUTO_SETUID_HOOK=y
CONFIG_KSU_MANUAL_HOOK_AUTO_INITRC_HOOK=y
CONFIG_KSU_MANUAL_HOOK_AUTO_INPUT_HOOK=y
'''


def patch_defconfig(tree, cfg_name):
    p = os.path.join(tree, 'arch', 'arm64', 'configs', cfg_name)
    if not os.path.exists(p):
        raise SystemExit('  [FAIL] defconfig not found: %s' % p)
    t = read(p)
    if 'CONFIG_KSU=y' in t:
        print('  [skip] defconfig')
        return
    if not t.endswith('\n'):
        t += '\n'
    t += CONFIGS
    write(p, t)
    print('  [ok]   %s' % cfg_name)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    tree = os.path.abspath(sys.argv[1])
    src = os.path.abspath(sys.argv[2])
    cfg = sys.argv[3] if len(sys.argv) > 3 else 'sprd_ums9157_defconfig'
    if not os.path.isdir(os.path.join(tree, 'drivers')):
        raise SystemExit('not a kernel tree: %s' % tree)
    if not os.path.isdir(os.path.join(src, 'hook')):
        raise SystemExit('not a BakaSU kernel dir: %s' % src)

    print('[1/5] copy source')
    copy_source(tree, src)
    print('[2/5] mount in drivers/')
    patch_kconfig(tree)
    patch_makefile(tree)
    print('[3/5] manual hooks')
    hook_stat(tree)
    hook_exec(tree)
    hook_faccessat(tree)
    hook_reboot(tree)
    hook_input(tree)
    print('[4/5] selinux symbol export')
    export_selinux(tree)
    print('[5/5] defconfig')
    patch_defconfig(tree, cfg)
    print('done.')


if __name__ == '__main__':
    main()
