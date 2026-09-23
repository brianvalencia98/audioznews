# Publicador RSS para Telegram con GitHub Actions

Este proyecto revisa una fuente RSS cada 10 minutos y publica en un canal de Telegram únicamente las entradas nuevas. No necesita un servidor encendido: GitHub Actions ejecuta el script, actualiza `estado.json` y guarda el cambio en el repositorio.

El mensaje usa HTML e incluye título, resumen, enlace directo e imagen si el RSS la proporciona.

## Archivos principales

- `bot.py`: revisa el RSS, prepara el mensaje y llama a la API de Telegram.
- `estado.json`: registra las publicaciones ya procesadas para evitar duplicados.
- `.github/workflows/telegram_rss.yml`: ejecuta el bot cada 10 minutos y permite iniciarlo manualmente.
- `.env.example`: plantilla para pruebas locales. El archivo `.env` real está ignorado por Git.

## 1. Subir el proyecto a GitHub

1. Crea un repositorio nuevo en GitHub. No añadas un README desde GitHub, porque este proyecto ya tiene uno.
2. Abre PowerShell en la carpeta del proyecto.
3. Ejecuta estos comandos, sustituyendo `TU_USUARIO` y `TU_REPOSITORIO`:

```powershell
git init
git add .
git commit -m "Primer commit: bot RSS para Telegram"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPOSITORIO.git
git push -u origin main
```

Antes de confirmar, comprueba que `.env` no aparezca en `git status`. Nunca subas el token del bot a GitHub.

## 2. Configurar GitHub Secrets

En tu repositorio de GitHub abre **Settings** > **Secrets and variables** > **Actions** > **New repository secret**. Crea estos tres Secrets:

| Nombre | Contenido |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | El token de BotFather. |
| `TELEGRAM_CHANNEL_ID` | `@usuario_del_canal` o el ID numérico que comienza por `-100`. |
| `RSS_URL` | La URL completa de la fuente RSS o Atom. |

No pongas estos datos directamente en `bot.py`, en el workflow ni en ningún commit. GitHub los entrega al workflow como variables de entorno solo durante su ejecución.

## 3. Agregar el bot al canal

1. Abre el canal de Telegram.
2. Ve a **Administrar canal** > **Administradores** > **Añadir administrador**.
3. Busca tu bot y concédele el permiso **Publicar mensajes**.
4. Guarda los cambios.

## 4. Primera ejecución manual

1. En GitHub abre la pestaña **Actions**.
2. Selecciona el workflow **Publicar RSS en Telegram**.
3. Pulsa **Run workflow** y confirma con el botón verde.
4. Revisa los registros de la ejecución.

En la primera ejecución el bot registra las entradas que ya existen en el RSS dentro de `estado.json`, pero no publica ninguna. Al finalizar, el workflow hará un commit automático con ese archivo. Las siguientes ejecuciones enviarán solo novedades.

## 5. Frecuencia de revisión

La programación está en `.github/workflows/telegram_rss.yml`:

```yaml
- cron: "*/10 * * * *"
```

Ese valor significa “cada 10 minutos” en UTC. Para cambiarla, edita el cron y haz commit. Por ejemplo, cada 30 minutos:

```yaml
- cron: "*/30 * * * *"
```

GitHub puede iniciar los trabajos programados con unos minutos de retraso en momentos de alta demanda; el workflow manual siempre está disponible desde **Actions**.

## Funcionamiento y recuperación de errores

- Las entradas nuevas se envían de la más antigua a la más reciente.
- Cada ID o enlace enviado queda en `estado.json`.
- Si falla el RSS o Telegram, el error aparece en los logs del workflow y `estado.json` no se marca incorrectamente como enviado.
- Si Telegram no puede descargar una imagen, el bot intenta enviar la misma publicación como texto.
- El workflow necesita `contents: write`, ya configurado, para hacer commit y push de `estado.json` usando el `GITHUB_TOKEN` temporal de Actions.

Si el paso **Guardar estado actualizado** muestra un error `403` al hacer `git push`, abre **Settings** > **Actions** > **General** y, en **Workflow permissions**, selecciona **Read and write permissions**. Después guarda el cambio y ejecuta el workflow de nuevo.

## Prueba local opcional

GitHub Actions no usa `.env`: usa exclusivamente los Secrets configurados en GitHub. El archivo `.env.example` solo es una plantilla local y `.gitignore` evita subir un `.env` real. Para ejecutar una prueba local, instala dependencias con:

```powershell
py -3.11 -m pip install -r requirements.txt
```

Luego define las tres variables de entorno de PowerShell y ejecuta:

```powershell
$env:TELEGRAM_BOT_TOKEN="TU_TOKEN"
$env:TELEGRAM_CHANNEL_ID="@tu_canal_o_id"
$env:RSS_URL="https://ejemplo.com/feed.xml"
python bot.py
```

No ejecutes pruebas locales con credenciales reales si no quieres publicar en el canal.
