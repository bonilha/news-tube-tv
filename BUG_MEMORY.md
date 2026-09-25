# Bug Memory — NewsTube TV

Bugs corrigidos que não deveriam ter passado. Referência para não repetir.

| # | Sintoma | Causa raiz |
|---|---------|------------|
| 1 | 4009 Auth Failed (OBS WebSocket) | `\r` (carriage return) de line endings Windows no `.env` sendo incluído no hash SHA-256. Algoritmo `_compute_auth` correto (bytes concatenation conforme spec). Fix: `.strip()` no password antes do hash. |
| 2 | 500 unhashable type: 'dict' | Starlette 1.6+ mudou assinatura de `TemplateResponse` — `request` virou primeiro argumento nomeado |
| 3 | "Checking OBS..." travado | HTMX ignora `load` em `display:none` + singleton OBS sem reconexão + JS engolindo eventos `beforeunload` |
| 4 | Arquivo abre no browser ao arrastar | Nenhum `e.preventDefault()` no `dragover` → comportamento padrão do browser ativado |
| 5 | Menu duplicado (dois `<aside>`) | Rotas retornam HTML completo (`{% extends "base.html" %}`) que inclui `<aside>`. HTMX injeta esse HTML inteiro dentro de `<main id="content">`. Fix: detectar header `HX-Request` no `_html()` e passar `htmx=True` ao template; template usa `{% if not htmx %}{% extends "base.html" %}{% endif %}`. |
