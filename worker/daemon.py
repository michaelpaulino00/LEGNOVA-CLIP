#!/usr/bin/env python3
"""LEGNOVA CLIP autonomous capture worker for media with proven reuse rights.

Requires persistent external compute (not the Base44 serverless runtime), a valid
WORKER_BRIDGE_KEY, and an explicit MEDIA_RIGHTS_CONFIRMED=1 setting.
"""
import hashlib
import json
import logging
import ipaddress
import socket
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
import yt_dlp
from highlight_engine import process

LOG=logging.getLogger('legnova-worker')

def start_http_health():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/healthz', '/'):
                self.send_response(404);self.end_headers();return
            healthy=bool(os.environ.get('WORKER_BRIDGE_KEY')) and os.environ.get('MEDIA_RIGHTS_CONFIRMED')=='1'
            payload=json.dumps({'service':'legnova-clip-worker','ready':healthy,'media_capture':'worker' if healthy else 'disabled'}).encode()
            self.send_response(200)  # Service is alive; JSON readiness remains false until credentials are set
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers();self.wfile.write(payload)
        def log_message(self,format,*args):return
    port=int(os.environ.get('PORT','10000'))
    server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    LOG.info('health endpoint ready on port %s',port)

BASE=os.environ.get('LEGNOVA_APP_URL','https://clip.legnovagroup.com').rstrip('/')
APP_ID='6aa55ed2720df82772df3706'
WORKER_KEY=os.environ.get('WORKER_BRIDGE_KEY','')
POLL_SECONDS=max(5,int(os.environ.get('POLL_SECONDS','10')))
LIVE_SECONDS=max(60,min(300,int(os.environ.get('LIVE_CAPTURE_SECONDS','180'))))
LIMIT_MB=max(200,min(2048,int(os.environ.get('SOURCE_MAX_MB','1000'))))

def bridge(action,**payload):
    url=f'{BASE}/functions/workerBridge'
    res=requests.post(url,json={'action':action,**payload},headers={'Authorization':'Bearer '+WORKER_KEY},timeout=25)
    res.raise_for_status()
    data=res.json()
    if not data.get('ok'):raise RuntimeError(data.get('error','Base44 bridge error'))
    return data

def media_id(url):
    p=urlparse(url)
    if p.hostname not in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'):raise ValueError('Solo se permiten enlaces a YouTube')
    from urllib.parse import parse_qs
    path=p.path.strip('/')
    live_match=re.fullmatch(r'live/([a-zA-Z0-9_-]{11})', path)
    value=path if p.hostname=='youtu.be' else (parse_qs(p.query).get('v',[''])[0] or (live_match.group(1) if live_match else ''))
    if not re.fullmatch(r'[a-zA-Z0-9_-]{11}',value):raise ValueError('ID de video inválido')
    return value

def verified_direct_url(url: str, kind: str) -> str:
    """Reject unsafe destinations; require an explicit domain allowlist for HLS."""
    p=urlparse(url)
    host=(p.hostname or '').lower().rstrip('.')
    allow={value.strip().lower().rstrip('.') for value in os.environ.get('AUTHORIZED_MEDIA_HOSTS','').split(',') if value.strip()}
    if p.scheme!='https' or not host or p.username or p.password or p.port not in (None,443):
        raise ValueError('La fuente directa debe ser una URL HTTPS sin credenciales en la dirección')
    if kind=='hls' and not allow:
        raise PermissionError('Para fuentes HLS debes configurar AUTHORIZED_MEDIA_HOSTS en Render')
    if allow and host not in allow:
        raise PermissionError('El dominio de transmisión no está autorizado por el administrador del motor')
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise PermissionError('El origen de la transmisión no puede ser una dirección privada o local')
    return url


def direct_capture(url: str, kind: str, folder: pathlib.Path):
    """Use a broadcaster-provided MP4 or HTTPS HLS feed instead of the YouTube webpage."""
    verified_direct_url(url,kind)
    if kind=='hls':
        target=folder/'source.mp4'
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
             '-protocol_whitelist','https,tls,tcp,crypto,http',
             '-i',url,'-t',str(LIVE_SECONDS),'-c:v','libx264','-preset','veryfast',
             '-crf','24','-c:a','aac','-b:a','128k','-movflags','+faststart',str(target)]
        subprocess.run(cmd,timeout=LIVE_SECONDS+90,check=True)
    elif kind in ('mp4','webm'):
        target=folder/('source.webm' if kind=='webm' else 'source.mp4')
        if kind=='webm':
            origin=urlparse(url)
            # The browser recorder may only feed the worker with a public Base44 file
            # uploaded under this app, never an arbitrary URL supplied as WebM.
            if origin.hostname!='base44.app' or not origin.path.startswith(
                    f'/api/apps/{APP_ID}/files/mp/public/{APP_ID}/'):
                raise PermissionError('Los fragmentos WebM deben almacenarse en Base44 para esta aplicación')
        # Base44's public file endpoint redirects to its media delivery domain.
        # Follow that specific trusted redirect, but never arbitrary untrusted hosts.
        current=url
        origin=urlparse(url)
        for hop in range(3):
            verified_direct_url(current,'mp4')
            with requests.get(current,stream=True,allow_redirects=False,timeout=(10,45)) as res:
                if res.status_code in (301,302,303,307,308):
                    from urllib.parse import urljoin
                    new=urljoin(current,res.headers.get('Location',''))
                    oldp,newp=urlparse(current),urlparse(new)
                    base44_handoff=(origin.hostname=='base44.app'
                        and origin.path.startswith(f'/api/apps/{APP_ID}/files/mp/public/{APP_ID}/')
                        and oldp.hostname=='base44.app' and newp.hostname=='media.base44.com'
                        and newp.path.startswith(f'/videos/public/{APP_ID}/')
                        and newp.path.rsplit('/',1)[-1]==origin.path.rsplit('/',1)[-1])
                    same_host=(newp.hostname==oldp.hostname)
                    if not (base44_handoff or same_host):
                        raise ValueError('La fuente intenta redirigir a un dominio de video no autorizado')
                    verified_direct_url(new,'mp4')
                    current=new
                    continue
                res.raise_for_status()
                content_type=res.headers.get('Content-Type','').split(';')[0].lower()
                accepted_types=('video/webm','application/octet-stream','binary/octet-stream') if kind=='webm' else ('video/mp4','application/octet-stream','binary/octet-stream')
                if content_type and content_type not in accepted_types:
                    raise ValueError('La fuente no devolvió un video '+kind.upper())
                length=int(res.headers.get('Content-Length') or 0)
                if length>LIMIT_MB*1024*1024:raise ValueError('El archivo supera el límite de tamaño')
                count=0
                with target.open('wb') as out:
                    for part in res.iter_content(chunk_size=1024*1024):
                        if not part:continue
                        count+=len(part)
                        if count>LIMIT_MB*1024*1024:raise ValueError('El video supera el límite de tamaño')
                        out.write(part)
                break
        else:
            raise ValueError('La fuente ha realizado demasiadas redirecciones')
    else:
        raise ValueError('Solo se acepta HLS, MP4 o WebM como fuente directa')
    if not target.is_file() or target.stat().st_size<100000:
        raise RuntimeError('No se recibieron datos de video suficientes')
    if kind=='webm':
        with target.open('rb') as stream:
            if stream.read(4)!=bytes.fromhex('1a45dfa3'):
                raise ValueError('La grabación no tiene cabecera WebM válida')
        converted=folder/'source.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
                        '-i',str(target),'-vf','scale=-2:480','-c:v','libx264',
                        '-preset','ultrafast','-crf','25','-threads','2',
                        '-c:a','aac','-b:a','112k','-movflags','+faststart',
                        str(converted)],check=True,timeout=360)
        target=converted
        LOG.info('Grabación WebM recibida y convertida a MP4 (%s bytes)',target.stat().st_size)
    return target

def youtube_auth_options():
    """Optional private owner-managed YouTube session held only in Render.

    Never accept YouTube cookies via public Base44 file uploads or print them.
    """
    filename=os.environ.get('YOUTUBE_COOKIE_FILE','').strip()
    if not filename:
        return {}
    path=pathlib.Path(filename)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size<100:
        raise RuntimeError('Archivo privado de sesión YouTube no configurado en Render')
    return {'cookiefile':str(path)}


def source_info(url):
    """Extract media with JS runtime and optional authorized owner session."""
    options={'quiet':True,'no_warnings':True,'noplaylist':True,
             'skip_download':True,'socket_timeout':20,
             'js_runtimes':{'deno':{}}}
    options.update(youtube_auth_options())
    with yt_dlp.YoutubeDL(options) as dl:
        return dl.extract_info(url,download=False)


def streamlink_capture(url, target):
    """Fallback public-live-stream reader. No login bypass or user credentials."""
    if not shutil.which('streamlink'):
        raise RuntimeError('Streamlink no está instalado en el procesador')
    stream_cmd=['streamlink','--loglevel','error','--retry-open','3',
                '--retry-streams','5','--retry-max','3','--stream-timeout','20',
                '--stdout',url,'480p,720p,best']
    auth=youtube_auth_options()
    if auth:
        stream_cmd[1:1]=['--http-cookies-file',auth['cookiefile']]
    transcode_cmd=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
                   '-i','pipe:0','-t',str(LIVE_SECONDS),'-vf','scale=-2:480',
                   '-threads','2','-c:v','libx264','-preset','veryfast','-crf','24',
                   '-c:a','aac','-b:a','128k','-movflags','+faststart',str(target)]
    with tempfile.TemporaryFile() as stream_errors:
        stream=subprocess.Popen(stream_cmd,stdout=subprocess.PIPE,stderr=stream_errors)
        try:
            try:
                result=subprocess.run(transcode_cmd,stdin=stream.stdout,stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE,timeout=LIVE_SECONDS+90,check=False)
            finally:
                if stream.stdout:
                    stream.stdout.close()
                if stream.poll() is None:
                    stream.terminate()
                try:
                    stream.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    stream.kill()
                    stream.wait(timeout=5)
            stream_errors.seek(0)
            stream_detail=stream_errors.read().decode('utf-8','replace')[-400:].strip()
            if result.returncode:
                ffmpeg_detail=result.stderr.decode('utf-8','replace')[-280:].strip()
                raise RuntimeError('Streamlink no obtuvo señal pública: '+stream_detail+
                                   '; ffmpeg: '+ffmpeg_detail)
            if not target.is_file() or target.stat().st_size<100000:
                raise RuntimeError('Streamlink no entregó video público utilizable: '+stream_detail)
            return target
        finally:
            if stream.poll() is None:
                stream.kill()
                stream.wait(timeout=5)

def live_capture(info,out):
    formats=info.get('formats') or []
    combined=[f for f in formats if str(f.get('protocol','')).startswith('m3u8') and f.get('vcodec') not in ('none',None) and f.get('acodec') not in ('none',None) and f.get('url')]
    if combined:
        choice=sorted(combined,key=lambda f:(f.get('height') or 0,f.get('tbr') or 0))[-1]
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-i',choice['url'],'-t',str(LIVE_SECONDS),'-c:v','libx264','-preset','veryfast','-crf','24','-c:a','aac','-b:a','128k','-movflags','+faststart',str(out)]
    else:
        video=[f for f in formats if str(f.get('protocol','')).startswith('m3u8') and f.get('vcodec') not in ('none',None) and f.get('url')]
        audio=[f for f in formats if str(f.get('protocol','')).startswith('m3u8') and f.get('acodec') not in ('none',None) and f.get('url')]
        if not video or not audio:raise RuntimeError('No se obtuvo una fuente HLS accesible con audio y video de esta transmisión. Se necesita acceso autorizado al stream.')
        v=sorted(video,key=lambda f:abs((f.get('height') or 720)-720))[0]['url']
        a=sorted(audio,key=lambda f:f.get('abr') or 0)[-1]['url']
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-i',v,'-i',a,'-map','0:v:0','-map','1:a:0','-t',str(LIVE_SECONDS),'-c:v','libx264','-preset','veryfast','-crf','24','-c:a','aac','-b:a','128k','-movflags','+faststart',str(out)]
    subprocess.run(cmd,check=True,timeout=LIVE_SECONDS+90)
    if not out.exists() or out.stat().st_size<100000:raise RuntimeError('La captura del directo no generó MP4 válido')
    return out

def archive_download(url,dest):
    options={'format':'bestvideo[height<=720]+bestaudio/best[height<=720]/best','merge_output_format':'mp4',
             'outtmpl':str(dest/'archive.%(ext)s'),'noplaylist':True,'quiet':True,'no_warnings':True,
             'max_filesize':LIMIT_MB*1024*1024,'socket_timeout':30,'retries':2,
             'js_runtimes':{'deno':{}}}
    options.update(youtube_auth_options())
    with yt_dlp.YoutubeDL(options) as dl:dl.download([url])
    matches=list(dest.glob('archive.*'))
    matches=[p for p in matches if p.suffix in ('.mp4','.mkv','.webm','.mov')]
    if not matches:raise RuntimeError('No se descargó un archivo multimedia del video')
    chosen=max(matches,key=lambda p:p.stat().st_size)
    if chosen.stat().st_size>LIMIT_MB*1024*1024:raise RuntimeError('El archivo supera el límite de tamaño')
    return chosen

def upload(session_id,video_id,record,index):
    path=pathlib.Path(record['video_path'])
    subtitle=pathlib.Path(record['subtitles_path'])
    sig=hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    key=f'{video_id}_{sig}'
    words=[x.strip() for x in subtitle.read_text(encoding='utf-8').splitlines() if x and '-->' not in x and not x.isdigit()]
    title=' '.join(' '.join(words).split()[:10]).strip()[:140] or f'Fragmento {index+1}'
    data={'action':'upload','session_id':session_id,'source_key':key,'start_seconds':str(record['start_seconds']),
          'end_seconds':str(record['end_seconds']),'title':title,'description':'Fragmento de transmisión autorizado'}
    with path.open('rb') as file:
        res=requests.post(f'{BASE}/functions/workerBridge',data=data,
            files={'file':(path.name,file,'video/mp4')},
            headers={'Authorization':'Bearer '+WORKER_KEY},timeout=180)
    res.raise_for_status()
    obj=res.json()
    if not obj.get('ok'):raise RuntimeError(obj.get('error','No se cargó el clip'))
    LOG.info('Clip registrado %s (duplicado=%s)',obj.get('clip_id'),obj.get('duplicate'))
    return obj

def process_session(session):
    session_id=session['id'];url=session['url'];video_id=media_id(url)
    if os.environ.get('MEDIA_RIGHTS_CONFIRMED')!='1':
        raise PermissionError('No se ha confirmado autorización de reutilización del contenido')
    source_url=str(session.get('ingest_url') or '').strip()
    source_kind=str(session.get('ingest_type') or 'hls')
    is_live=source_kind=='hls' if source_url else False
    with tempfile.TemporaryDirectory(prefix='legnova_') as tmp:
        root=pathlib.Path(tmp)
        if source_url:
            source=direct_capture(source_url,source_kind,root)
        else:
            try:
                info=source_info(url)
                allowed=os.environ.get('ALLOWED_YOUTUBE_CHANNEL_ID','').strip()
                source_channel=str(info.get('channel_id') or '')
                if allowed and source_channel!=allowed:
                    raise PermissionError('El video pertenece a un canal diferente al autorizado')
                is_live=bool(info.get('is_live'))
                LOG.info('YouTube source identified: %s, live=%s',video_id,is_live)
                source=live_capture(info,root/'source.mp4') if is_live else archive_download(url,root)
            except PermissionError:
                raise
            except Exception as primary_error:
                LOG.warning('yt-dlp could not open YouTube video %s: %s; trying Streamlink',
                            video_id,str(primary_error)[:250])
                try:
                    source=streamlink_capture(url,root/'streamlink_capture.mp4')
                    is_live=True
                    LOG.info('Streamlink captured video from %s',video_id)
                except Exception as backup_error:
                    raise RuntimeError('YouTube no entregó video en los dos lectores públicos. '
                                       'yt-dlp: '+str(primary_error)[:230]+
                                       '; Streamlink: '+str(backup_error)[:160]) from backup_error
        manifest=process(source,root/'clips',os.environ.get('WHISPER_MODEL','small'),30,80,5)
        count=0
        for idx,clip in enumerate(manifest['clips']):
            upload(session_id,video_id,clip,idx)
            count+=1
        if is_live:
            bridge('release',session_id=session_id)
        else:
            bridge('complete',session_id=session_id)
        return count

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    start_http_health()
    if len(WORKER_KEY)<32:
        LOG.warning('WORKER_BRIDGE_KEY is missing. Service online but capture disabled until credentials are configured.')
        while True:time.sleep(120)
    if os.environ.get('MEDIA_RIGHTS_CONFIRMED')!='1':raise PermissionError('Confirma primero derechos de reutilización del contenido para ejecutar el worker')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):raise RuntimeError('FFmpeg y FFprobe no están disponibles')
    LOG.info('LEGNOVA worker started; polling Base44 (Deno=%s, Streamlink=%s)',
             bool(shutil.which('deno')),bool(shutil.which('streamlink')))
    last_ping=0
    while True:
        try:
            if time.monotonic()-last_ping>60:
                bridge('heartbeat',worker_name='legnova-video-worker',version='0.2')
                last_ping=time.monotonic()
            work=bridge('poll')['sessions']
            if work:
                session=work[0]
                bridge('claim',session_id=session['id'])
                try:
                    n=process_session(session)
                    LOG.info('Session %s: %d clips registered',session['id'],n)
                except Exception as exc:
                    LOG.exception('Session %s failed',session['id'])
                    issue=str(exc)
                    denied=any(term in issue.lower() for term in ('sign in to confirm', 'not a bot', 'login_required', 'sign in to youtube'))
                    if denied:
                        bridge('needs_source',session_id=session['id'],error='YouTube requiere inicio de sesión en el servidor. Los dos lectores públicos fallaron; se reintentará automáticamente en 15 minutos.')
                    else:
                        bridge('failure',session_id=session['id'],error=issue[:300])
        except Exception:
            LOG.exception('Polling or capture failed')
        time.sleep(POLL_SECONDS)

if __name__=='__main__':main()