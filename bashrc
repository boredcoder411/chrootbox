# skip prompt setup when bash is not interactive
case $- in
    *i*) ;;
    *) return ;;
esac

# show the current directory in the terminal prompt
PS1='\[\e[90m\]chrootbox \[\e[94m\]\w \[\e[92m\]# \[\e[0m\]'

if [[ -r /etc/motd ]]; then
    cat /etc/motd
fi
