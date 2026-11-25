#!/usr/bin/env python3
"""
sudo apt install git build-essential autoconf yacc -y
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPOS = {
    'sbase': 'https://git.suckless.org/sbase',
    'tcc': 'https://repo.or.cz/tinycc.git',
    'vim': 'https://github.com/vim/vim.git',
}

def run(*args, cwd=None, env=None, capture=False):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                          check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout

def copy_host_path(path, root):
    path = Path(os.path.abspath(path))
    if not path.exists() and not path.is_symlink():
        return
    # keep each symlink in the rootfs and copy its eventual target too
    seen = set()
    while True:
        name = str(path)
        if name in seen:
            raise RuntimeError('symlink cycle: ' + name)
        seen.add(name)
        dest = root / name.lstrip('/')
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            target = os.readlink(path)
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            dest.symlink_to(target)
            path = (Path(target) if target.startswith('/')
                    else path.parent / target)
            path = Path(os.path.normpath(path))
            continue
        if path.is_dir():
            shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
        elif path.is_file():
            if not dest.exists():
                shutil.copy2(path, dest)
        break

def install_bash(root):
    # copy over host bash, no compiling this
    bash = shutil.which('bash')
    if bash is None:
        raise RuntimeError('missing host bash')
    shutil.copy2(Path(bash).resolve(), root / 'usr/bin/bash')
    link = root / 'bin/bash'
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to('../usr/bin/bash')


def install_shell_config(root):
    source = Path(__file__).resolve().parent
    shutil.copy2(source / 'bashrc', root / 'root/.bashrc')
    shutil.copy2(source / 'motd', root / 'etc/motd')


def elf_info(path):
    try:
        # readelf gives the loader path and direct shared library names
        out = subprocess.run(['readelf', '-l', '-d', str(path)],
                             text=True, capture_output=True)
        if out.returncode != 0 or 'ELF' not in path.open('rb').read(4).decode('latin1'):
            return None
        interp = re.findall(r'Requesting program interpreter: ([^\]]+)', out.stdout)
        needed = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', out.stdout)
        return interp, needed
    except (OSError, UnicodeError):
        return None

def dependencies(path):
    # ldd resolves library names to the host paths needed in the rootfs
    out = subprocess.run(['ldd', str(path)], text=True, capture_output=True)
    if out.returncode:
        if 'not a dynamic executable' in out.stdout + out.stderr or 'statically linked' in out.stdout + out.stderr:
            return []
        raise RuntimeError(f'ldd failed for {path}: {out.stdout} {out.stderr}')
    if 'not found' in out.stdout:
        raise RuntimeError(f'unresolved dependency for {path}:\n{out.stdout}')
    paths = []
    for line in out.stdout.splitlines():
        m = re.search(r'=>\s+(/\S+)', line)
        if m:
            paths.append(m.group(1))
        else:
            m = re.match(r'\s*(/\S+)\s+\(', line)
            if m:
                paths.append(m.group(1))
    return paths

def is_elf(path):
    try:
        with path.open('rb') as f:
            return f.read(4) == b'\x7fELF'
    except OSError:
        return False

def install_runtime(root):
    visited = set()
    # copied libraries may need more libraries, so scan until none are new
    while True:
        pending = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                   and is_elf(p) and str(p.relative_to(root)) not in visited]
        if not pending:
            break
        for p in pending:
            visited.add(str(p.relative_to(root)))
            info = elf_info(p)
            if info:
                for interpreter in info[0]:
                    copy_host_path(interpreter, root)
            for lib in dependencies(p):
                copy_host_path(lib, root)


def copy_development_files(root):
    # tcc needs host libc-dev files
    include = Path('/usr/include')
    if not include.is_dir():
        raise RuntimeError('missing /usr/include (install libc development headers)')
    shutil.copytree(include, root / 'usr/include', symlinks=True, dirs_exist_ok=True)
    cc = os.environ.get('CC', 'cc')
    for name in ('crt1.o', 'Scrt1.o', 'rcrt1.o', 'crti.o', 'crtn.o',
                 'libc.so', 'libm.so', 'libpthread.so', 'libdl.so',
                 'librt.so', 'libresolv.so', 'libgcc_s.so.1'):
        # ask the host compiler where its startup objects and libraries live
        resolved = run(cc, '-print-file-name=' + name, capture=True).strip()
        if resolved and resolved != name and Path(resolved).exists():
            copy_host_path(resolved, root)
    # gcc intrinsic headers can be useful?
    inc = run(cc, '-print-file-name=include', capture=True).strip()
    if Path(inc).is_dir():
        copy_host_path(inc, root)
    # some libc files aren't absolute, copy their targets
    for p in list(root.rglob('*.so')):
        if p.is_file() and not p.is_symlink() and not is_elf(p):
            try:
                text = p.read_text()
            except (OSError, UnicodeError):
                continue
            if 'GROUP' in text or 'INPUT' in text:
                for target in re.findall(r'(?<![\w])(/(?:usr/)?lib[^\s(),]+)', text):
                    copy_host_path(target, root)

    # name service modules are loaded at runtime and may not appear in ldd
    for base in ('/lib', '/lib64', '/usr/lib', '/usr/lib64'):
        directory = Path(base)
        if directory.is_dir():
            for pattern in ('libnss_files.so*', 'libnss_dns.so*', 'libresolv.so*'):
                for p in directory.rglob(pattern):
                    if p.is_file() or p.is_symlink():
                        copy_host_path(p, root)


def build(name, src, root, jobs):
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    if name == 'sbase':
        # remove tftp/tftpd from sbase makefile
        mk = src / 'Makefile'
        text = mk.read_text()
        match = re.search(r'(?ms)^BIN\s*[:+?]?=\s*\\\n.*?(?=^[A-Za-z_.-]+\s*[:+?]?=|^[^\t#\s][^\n]*:|\Z)', text)
        if not match:
            raise RuntimeError('unrecognized sbase Makefile BIN layout; refusing to include tftp')
        block = match.group(0)
        changed = re.sub(r'(?<![\w-])(?:tftp|tftpd)(?![\w-])', '', block)
        mk.write_text(text[:match.start()] + changed + text[match.end():])

        run('make', f'-j{jobs}', 'PREFIX=/usr', cwd=src, env=env)
        run('make', 'install', 'PREFIX=/usr', f'DESTDIR={root}', cwd=src, env=env)
        for forbidden in ('tftp', 'tftpd'):
            for folder in ('bin', 'usr/bin'):
                p = root / folder / forbidden
                if p.exists() or p.is_symlink():
                    p.unlink()
    elif name == 'tcc':
        run('./configure', '--prefix=/usr', cwd=src, env=env)
        run('make', f'-j{jobs}', cwd=src, env=env)
        run('make', 'install', f'DESTDIR={root}', cwd=src, env=env)
    elif name == 'vim':
        # keep vim small and avoid optional language and GUI dependencies
        vsrc = src / 'src'
        run('./configure', '--prefix=/usr', '--with-features=tiny',
            '--without-x', '--disable-gui', '--disable-nls',
            '--disable-perlinterp', '--disable-pythoninterp',
            '--disable-python3interp', '--disable-rubyinterp',
            '--disable-luainterp', '--disable-tclinterp',
            '--disable-cscope', '--disable-netbeans',
            cwd=vsrc, env=env)
        run('make', f'-j{jobs}', cwd=vsrc, env=env)
        run('make', 'install', f'DESTDIR={root}', cwd=vsrc, env=env)
        vi = root / 'usr/bin/vi'
        vi.parent.mkdir(parents=True, exist_ok=True)
        if vi.exists() or vi.is_symlink():
            vi.unlink()
        vi.symlink_to('vim')


def prepare(root):
    # create the basic filesystem layout before installing programs
    for name in ('bin', 'usr/bin', 'usr/lib', 'lib', 'lib64', 'etc', 'tmp',
                 'var/tmp', 'root', 'home', 'dev', 'proc', 'sys', 'run'):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ('tmp', 'var/tmp'):
        (root / name).chmod(0o1777)
    (root / 'root').chmod(0o700)
    # the shell needs a root account and local name lookup inside the chroot
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (root / 'etc/group').write_text('root:x:0:\n')
    (root / 'etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nhosts: files dns\n')
    (root / 'etc/hosts').write_text('127.0.0.1 localhost\n::1 localhost\n')
    (root / 'etc/resolv.conf').write_text('')
    (root / 'etc/profile').write_text('export PATH=/usr/bin:/bin\nexport HOME=/root\nexport TERM=${TERM:-xterm}\n')


def archive(root, output):
    # preserve symlinks so absolute paths still work after extraction
    with tarfile.open(output, 'w:gz', dereference=False, format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(root.rglob('*')):
            tar.add(path, arcname=str(path.relative_to(root)), recursive=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--output', type=Path, default=Path('chroot-host.tgz'))
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 2)
    parser.add_argument('--keep-build', type=Path, help='retain sources and staging root here')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Linux host required')
    for cmd in ('git', 'make', 'cc', 'readelf', 'ldd', 'bash'):
        if shutil.which(cmd) is None:
            parser.error(f'missing build tool: {cmd}')
    if args.keep_build:
        work = args.keep_build.resolve()
        if work.exists() and any(work.iterdir()):
            parser.error('--keep-build directory must be empty')
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        # temporary sources and staging files are removed after packaging
        work = Path(tempfile.mkdtemp(prefix='chroot-build-'))
        cleanup = True
    root = work / 'rootfs'
    root.mkdir()
    output = args.output.resolve()
    try:
        prepare(root)
        # build each program into the staged root, then add host runtime files
        for name, url in REPOS.items():
            src = work / name
            run('git', 'clone', '--depth', '1', url, src)
            build(name, src, root, args.jobs)
        # Standard shell path and fallback for tools expecting /bin/sh.
        sh = root / 'bin/sh'
        if sh.exists() or sh.is_symlink():
            sh.unlink()
        sh.symlink_to('../usr/bin/bash')
        install_bash(root)
        install_shell_config(root)
        copy_development_files(root)
        install_runtime(root)
        # sometimes removing tftp fucks up, ensure its not installed
        for name in ('tftp', 'tftpd'):
            if any(p.name == name for p in root.rglob('*')):
                raise RuntimeError(f'excluded executable found in rootfs: {name}')
        for path in ('usr/bin/bash', 'usr/bin/tcc', 'usr/bin/vi'):
            # fail before archiving if a required program was not installed
            if not (root / path).exists():
                raise RuntimeError(f'missing expected program: {path}')
        output.parent.mkdir(parents=True, exist_ok=True)
        archive(root, output)
        print(f'\ncreated {output} ({output.stat().st_size:,} bytes)')
    finally:
        if cleanup:
            shutil.rmtree(work)

if __name__ == '__main__':
    main()
