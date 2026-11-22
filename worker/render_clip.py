#!/usr/bin/env python3
"""LEGNOVA CLIP: prueba local de render de video autorizado.
Uso: python render_clip.py --input video.mp4 --start 0 --duration 30 --subtitles captions.srt --output clip.mp4
No descarga YouTube, no publica ni accede a Base44. Requiere ffmpeg/ffprobe.
"""
import argparse, pathlib, shutil, subprocess, sys

def run(args):
    subprocess.run(args, check=True, timeout=600)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--subtitles")
    p.add_argument("--start", type=float, default=0)
    p.add_argument("--duration", type=float, default=30)
    a=p.parse_args()
    if not shutil.which("ffmpeg"): p.error("Se requiere ffmpeg instalado")
    src=pathlib.Path(a.input).resolve()
    if not src.is_file(): p.error("No existe el archivo de entrada")
    if a.duration<=0 or a.duration>180: p.error("Duración permitida: 0-180 segundos")
    dest=pathlib.Path(a.output).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    if a.subtitles:
        s=pathlib.Path(a.subtitles).resolve()
        if not s.is_file(): p.error("No existe el SRT")
        # Subtítulos centrados y elevados para no tapar controles de las redes.
        escaped=str(s).replace("\\","/").replace(":","\\:").replace("'","\\'")
        vf+=f",subtitles='{escaped}':force_style='FontSize=18,Outline=2,Shadow=1,MarginV=230,Alignment=2'"
    cmd=["ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(a.start),"-i",str(src),"-t",str(a.duration),"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-movflags","+faststart",str(dest)]
    run(cmd)
    print(f"MP4 generado: {dest} ({dest.stat().st_size} bytes)")

if __name__=="__main__":
    try: main()
    except subprocess.CalledProcessError as e: sys.exit(f"FFmpeg falló: {e}")