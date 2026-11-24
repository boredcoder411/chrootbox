case $- in
    *i*) ;;
    *) return ;;
esac

PS1='\[\e[90m\]chrootbox \[\e[94m\]\w \[\e[92m\]# \[\e[0m\]'

if [[ -r /etc/motd ]]; then
    cat /etc/motd
fi
