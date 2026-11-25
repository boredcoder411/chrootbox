import fcntl
import os
from pathlib import Path
import subprocess
import sys
import termios


def main():
    root = Path(sys.argv[1])
    # mount special devices to avoid reimplementing a vfs
    for name in ('null', 'zero', 'random', 'urandom', 'tty'):
        target = root / 'dev' / name
        target.touch()
        subprocess.run(['mount', '--bind', f'/dev/{name}', str(target)], check=True)
    # give bash a controlling terminal on the PTY passed by the parent
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    # enter the staged filesystem before replacing this process with bash
    os.chroot(root)
    os.chdir('/root')
    os.environ['SHELL'] = '/bin/bash'
    os.execv('/bin/bash', ['/bin/bash', '-i'])


if __name__ == '__main__':
    main()
