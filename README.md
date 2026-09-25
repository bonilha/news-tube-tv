# NewsTube TV

Console do operador. Fila de vídeos do YouTube, download com yt-dlp e cenas no OBS.

## Setup

1. Python 3.11 ou mais novo.
2. Instale as dependências:

```
pip install -r requirements.txt
```

3. Copie `.env.example` para `.env` e preencha a senha do OBS e a URL do Invidious.
4. Exporte os cookies do YouTube em formato Netscape e salve em `cookies/cookiesyoutube.txt`.
5. Abra o OBS Studio com o servidor WebSocket ligado (porta 4455 por padrão).
6. Na pasta do projeto:

```
python main.py
```

O console fica em `http://127.0.0.1:8000`. O usuário e a senha vêm de `ADMIN_USER` e `ADMIN_PASSWORD` no `.env`.

`data.db`, `cookies/`, `videos/` e `assets/` ficam só na máquina. Não entram no git.
