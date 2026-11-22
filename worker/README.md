# LEGNOVA CLIP — procesador y automatización

## Estado comprobado

- La aplicación Base44 guarda los canales y transmisiones, verifica la fuente y mantiene workflows de monitoreo.
- El backend Base44 **no contiene ffmpeg ni python3** y `Deno.Command` no está habilitado (prueba interna realizada). Por ello **NO puede renderizar clips ni capturar un directo dentro de sus funciones**.
- Este directorio tiene un render básico probado con FFmpeg (MP4 1080x1920), un motor local con reconocimiento de voz Whisper y selección de candidatos por heurísticas, y un daemon que puede conectarse a Base44 mediante `workerBridge`.
- No hay un servidor persistente desplegado. No hay clip real generado a partir del canal del usuario ni publicaciones verificadas en TikTok.

## Motor local (archivos propios o con licencia)

Instala Python 3.12, FFmpeg y `pip install -r worker/requirements.txt`.

```bash
python worker/highlight_engine.py --input video.mp4 --output clips/ --min-seconds 30 --max-seconds 80 --max-clips 5
```

El resultado incluye MP4 verticales, subtítulos SRT y `manifest.json`. Whisper realiza la transcripción. La elección de momentos **todavía es heurística**, no un análisis editorial generativo. Requiere hablar en español en la fuente.

Para probar únicamente el render sin Whisper:

```bash
python worker/engine.py --input video.mp4 --output clips/ --clip-seconds 45 --max-clips 2
```

## Servicio automático Docker

El Dockerfile incluye FFmpeg, Python, yt-dlp y Whisper y arranca `daemon.py`. El daemon obtiene una sesión pendiente desde Base44, reclama el trabajo, obtiene el video cuando YouTube concede acceso, transcribe, renderiza y envía el MP4 a `workerBridge`. Los videos quedan guardados en Base44 como `Clip.status=ready`. El workflow `PublishNewClipTikTok` intenta publicarlos automáticamente en TikTok cuando la opción `auto_publish`, `tiktok_enabled` y el permiso `reuse_authorized` están activos, y TikTok permite `PUBLIC_TO_EVERYONE`. `ConfirmTikTokPublication` verifica su estado tras 5, 10 y 15 minutos y solo marca `published` tras la confirmación API.

```bash
docker build -f worker/Dockerfile -t legnova-clip .
docker run --restart unless-stopped --env-file worker/.env.private legnova-clip
```

El servicio debe desplegarse en un servidor persistente con acceso externo, recursos y almacenamiento temporal suficientes, no en el sandbox de desarrollo de Base44.

**Antes de arrancar:** establecer el mismo secreto largo y aleatorio `WORKER_BRIDGE_KEY` en Base44 → Secrets y en `worker/.env.private`; configurar `MEDIA_RIGHTS_CONFIRMED=1` solo si se posee el contenido o autorización expresa; limitar `ALLOWED_YOUTUBE_CHANNEL_ID` al canal autorizado. Las solicitudes al puente requieren HTTPS y token privado. Ningún secreto debe subirse a Git ni enviarse por chat.

El endpoint de Base44 `workerBridge` debe comprobarse con una prueba real desde el servidor: acción `poll`, `claim`, carga multipart MP4, registro en Clip, y finalización. No se ha probado ese flujo desde un host externo.

## Lo que resta para publicar en producción

1. Confirmar los derechos de reutilización del canal/stream.
2. Desplegar el contenedor en cómputo persistente y configurar credenciales privadas.
3. Verificar que YouTube autorice acceso al stream; YouTube Data API por sí sola no proporciona la descarga del video.
4. Ejecutar una prueba completa de captura, subtítulos, subida y reproducción en Base44. Corregir formatos/cortes antes de habilitar publicación.
5. Validar en producción la subida de MP4 a TikTok mediante `FILE_UPLOAD` (código integrado) y la autorización de publicación pública del app de TikTok; aún no se ha probado con un clip del canal conectado.
6. Confirmar que la app TikTok tiene acceso a publicación directa fuera de sus limitaciones Sandbox, permisos vigentes y privacidad deseada.
7. Agregar renovación de trabajo tras fallas de red y límites de recursos si se va a procesar en vivo de forma continuada.

No declarar `published` hasta que TikTok devuelva un estado final confirmado.

## Plantilla Render

`render.yaml` define un **background worker** Docker en Render. No es un servidor web: el proceso es continuo y revisa trabajos de Base44. Antes de aplicarla, enlaza el código con un repositorio Git accesible a Render, revisa los costes del plan `starter` y completa las variables privadas. La conexión del plugin Render con ChatGPT por sí sola no otorga acceso al repositorio Git del proyecto ni crea su despliegue.

El worker requiere un `WORKER_BRIDGE_KEY` idéntico al secreto homónimo de Base44, definido por el propietario en ambos paneles. `MEDIA_RIGHTS_CONFIRMED=1` y el campo `reuse_authorized=true` de la sesión solo deben habilitarse si existen los permisos correspondientes. No guardes secretos en el repositorio.

## Pruebas técnicas (22/11/2026)

En el sandbox de desarrollo se generó un video **original sintético** de 39.2 segundos con voz sintetizada, se transcribió mediante faster-whisper (`tiny`, detección automática de idioma), se eligió una ventana de 24.4 segundos, se creó un SRT y se renderizó un MP4 1080x1920 de ~3.8 MB. La prueba local terminó correctamente. Esta prueba **no demuestra** que el directo de YouTube pueda descargarse ni que TikTok acepte publicación pública.

Al consultar únicamente metadatos del video de YouTube inscrito mediante yt-dlp, YouTube respondió `Sign in to confirm you’re not a bot`. El usuario deberá obtener una fuente autorizada y técnicamente accesible; no se intenta saltar restricciones de la plataforma.

## Acceso al directo sin autenticación de YouTube

Cuando YouTube limita el acceso al reproductor con `LOGIN_REQUIRED`, la aplicación **no puede forzar ni saltarse esa verificación**. Como alternativa legítima, el propietario o distribuidor autorizado del directo puede proporcionar una **fuente HTTPS HLS (.m3u8)** para capturar mientras transmite, o un MP4 para procesar un archivo.

En LEGNOVA CLIP, se registra la URL de YouTube para identificación y la fuente directa por separado para la captura. El servicio debe configurar `AUTHORIZED_MEDIA_HOSTS` con los nombres exactos de los dominios autorizados, separados por coma. El worker rechaza hosts no listados y direcciones privadas. `MEDIA_RIGHTS_CONFIRMED` y el permiso individual siguen siendo obligatorios.

El flujo automático revisa los trabajos cada 10 segundos si el servicio está encendido; los ciclos HLS producen clips cuando haya suficiente audio acumulado y el análisis concluya. No puede garantizar un clip en el primer segundo porque necesita capturar y transcribir un fragmento.