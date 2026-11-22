#!/usr/bin/env python3
"""LEGNOVA CLIP: pipeline real para archivos locales propios/autorizados.
No descarga contenido de YouTube ni publica en redes sin aprobación.
"""
import argparse, json, pathlib, shutil, subprocess, sys, time
from render_clip import run

def duration(path):
    p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,check=True,timeout=30)
    return float(p.stdout.strip())

def process(src, out, clip_seconds, max_clips):
    src=pathlib.Path(src).resolve(); out=pathlib.Path(out).resolve()
    if not src.is_file(): raise ValueError('No existe el video fuente')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): raise RuntimeError('FFmpeg y FFprobe son obligatorios')
    if not 15<=clip_seconds<=180 or not 1<=max_clips<=20: raise ValueError('Límites de clips inválidos')
    length=duration(src)
    if length<15: raise ValueError('El video debe durar al menos 15 segundos')
    out.mkdir(parents=True,exist_ok=True)
    # Segmentación uniforme provisional, NO selección de momentos mediante IA.
    segments=min(max_clips,int(length//clip_seconds) or 1)
    manifest={'source':str(src),'source_duration':length,'selection_method':'uniform_provisional_not_ai','created_at':int(time.time()),'clips':[]}
    for i in range(segments):
        start=i*clip_seconds; length_here=min(clip_seconds,length-start)
        if length_here<15: break
        target=out/f'clip_{i+1:03d}.mp4'
        run([sys.executable,str(pathlib.Path(__file__).with_name('render_clip.py')),'--input',str(src),'--output',str(target),'--start',str(start),'--duration',str(length_here)])
        if target.stat().st_size==0: raise RuntimeError('FFmpeg creó un archivo vacío')
        manifest['clips'].append({'path':str(target),'start_seconds':start,'end_seconds':start+length_here,'bytes':target.stat().st_size,'status':'rendered_not_published'})
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    return manifest

if __name__=='__main__':
    ap=argparse.ArgumentParser(description='Renderizar clips verticales de un video autorizado')
    ap.add_argument('--input',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--clip-seconds',type=int,default=45);ap.add_argument('--max-clips',type=int,default=5)
    a=ap.parse_args()
    try: print(json.dumps(process(a.input,a.output,a.clip_seconds,a.max_clips),ensure_ascii=False,indent=2))
    except Exception as e: sys.exit('Error: '+str(e))