# chrootbox

## installation

commande pour tout installer sur debian:
```sh
sudo apt update
sudo apt install python3 python3-venv git build-essential autoconf bison binutils libncurses-dev util-linux tar bash
```

## execution

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python build_chroot.py
python app.py
```

ouvrir `http://127.0.0.1:5000` dans un navigateur
