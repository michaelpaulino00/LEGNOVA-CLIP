#!/usr/bin/env python3
"""LEGNOVA CLIP: local/offline clipping pipeline for user-owned licensed videos.

Uses faster-whisper for spoken-word timestamps and an explicit heuristic selector.
The selector is NOT generative AI. Does not fetch video or publish itself.
"""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable
from render_clip import run

INTERESTING = {
    'increible': 3, 'increíble': 3, 'exclusiva': 4, 'exclusivo': 4, 'noticia': 2,
    'atencion': 2, 'atención': 2, 'revela': 3, 'reveló': 3, 'confirmo': 2,
    'confirmó': 2, 'verdad': 2, 'escandalo': 3, 'escándalo': 3, 'sorpresa': 2,
    'denuncia': 2, 'problema': 1, 'importante': 2, 'ultima': 1, 'última': 1,
    'nunca': 2, 'ahora': 1, 'nadie': 1, 'nuevo': 1, 'último': 1,
    'crazy': 3, 'insane': 3, 'unbelievable': 3, 'impossible': 2,
    'finally': 2, 'secret': 3, 'incredible': 3, 'shocking': 3, 'wow': 2,
    'no': 1, 'way': 1, 'breaking': 2, 'amazing': 2, 'wait': 1,
}

@dataclass(frozen=True)
class Utterance:
    start: float
    end: float
    text: str


def probe(video: pathlib.Path) -> float:
    raw = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(video)],
        text=True, timeout=30)
    length = float(raw.strip())
    if not 15 < length < 24 * 3600:
        raise ValueError('El archivo no tiene una duración válida (15 s a 24 h)')
    return length


def transcribe(video: pathlib.Path, model_name: str) -> list[Utterance]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError('Instala faster-whisper: pip install -r worker/requirements.txt') from exc
    model = WhisperModel(model_name, device='cpu', compute_type='int8')
    segments, info = model.transcribe(str(video), language=None, vad_filter=True,
        beam_size=3, condition_on_previous_text=False)
    return [Utterance(float(s.start), float(s.end), s.text.strip())
        for s in segments if s.text.strip() and s.end > s.start]


def score(items: list[Utterance]) -> float:
    text = ' '.join(x.text for x in items)
    words = re.findall(r'\b[\wáéíóúñü]+\b', text.lower())
    if len(words) < 18:
        return -999
    weight = sum(INTERESTING.get(word, 0) for word in words)
    punctuation = min(text.count('!') * 1.5 + text.count('?') * 1.3, 6)
    spoken = items[-1].end - items[0].start
    density = min(len(words) / max(spoken, 1), 3)
    # Penalize repetition and silence. Engagement scoring is preliminary, not AI.
    distinct = len(set(words)) / max(len(words), 1)
    return round(weight + punctuation + density * 2 + distinct * 4, 3)


def rank_windows(utterances: list[Utterance], duration: float,
                 min_seconds: float, max_seconds: float, max_clips: int):
    possible=[]
    for i, begin in enumerate(utterances):
        if begin.start > duration-min_seconds: break
        for j in range(i, len(utterances)):
            end=utterances[j].end
            if end-begin.start>max_seconds: break
            if end-begin.start>=min_seconds:
                chunk=utterances[i:j+1]
                points=score(chunk)
                if points > -999:
                    possible.append((points,begin.start,end,i,j))
    possible.sort(key=lambda t:(-t[0],t[1]))
    selected=[]
    for points,start,end,i,j in possible:
        if any(max(start,s)<min(end,e) for _,s,e,_,_ in selected):continue
        selected.append((points,start,end,i,j))
        if len(selected)>=max_clips:break
    return sorted(selected,key=lambda item:item[1])


def srt_time(seconds: float) -> str:
    milliseconds=max(0,int(round(seconds*1000)))
    h,ms=divmod(milliseconds,3600000)
    m,ms=divmod(ms,60000)
    s,ms=divmod(ms,1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def make_srt(utterances: Iterable[Utterance], offset: float, end: float) -> str:
    blocks=[]
    for item in utterances:
        start=max(0,item.start-offset)
        stop=min(end-offset,item.end-offset)
        if stop<=start:continue
        words=item.text.split()
        if not words:continue
        # Small subtitles: at most six words per card; timing proportional.
        for k in range(0,len(words),6):
            a=k/len(words);b=min(k+6,len(words))/len(words)
            substart=start+(stop-start)*a
            subend=start+(stop-start)*b
            blocks.append(f'{len(blocks)+1}\n{srt_time(substart)} --> {srt_time(subend)}\n{" ".join(words[k:k+6])}\n')
    return '\n'.join(blocks)


def process(video: pathlib.Path, output: pathlib.Path, model: str,
            min_seconds: int, max_seconds: int, max_clips: int):
    video=video.resolve();output=output.resolve()
    if not video.is_file():raise FileNotFoundError(video)
    if min_seconds < 15 or max_seconds > 180 or min_seconds>max_seconds or max_clips not in range(1,21):
        raise ValueError('Duración permitida: 15-180 segundos, máximo 20 clips')
    length=probe(video)
    output.mkdir(parents=True,exist_ok=True)
    spoken=transcribe(video,model)
    picks=rank_windows(spoken,length,min_seconds,max_seconds,max_clips) if spoken else []
    # Video promos/trailers may have music and sparse dialogue. In this case
    # produce bounded highlights without claiming the heuristic found speech.
    fallback=not bool(picks)
    if fallback:
        segment=min(float(max_seconds),max(float(min_seconds),min(45.0,length/2)))
        start_points=[0.0]
        if length>segment+15 and max_clips>1:
            start_points.append(max(0.0,length-segment))
        picks=[(0.0,t,min(length,t+segment),0,-1) for t in start_points]
    signature=hashlib.sha256((str(video)+str(video.stat().st_size)).encode()).hexdigest()[:16]
    records=[]
    for points,start,end,i,j in picks:
        name=f'{signature}_{int(start*1000):010d}_{int(end*1000):010d}'
        target=output/(name+'.mp4')
        subtitle=output/(name+'.srt')
        subtitle.write_text(make_srt(spoken[i:j+1],start,end) if not fallback else '',encoding='utf-8')
        if not target.is_file() or target.stat().st_size == 0:
            command=[sys.executable,str(pathlib.Path(__file__).with_name('render_clip.py')),
                '--input',str(video),'--output',str(target),'--start',str(start),
                '--duration',str(end-start)]
            if not fallback and subtitle.stat().st_size:command+=['--subtitles',str(subtitle)]
            run(command)
        if target.stat().st_size==0:raise RuntimeError('El render quedó vacío')
        records.append({'video_path':str(target),'subtitles_path':str(subtitle),
            'score':points,'start_seconds':start,'end_seconds':end,
            'selection_method':'time_fallback_no_speech' if fallback else 'whisper_transcription_and_heuristics','status':'ready_local_not_published'})
    manifest={'source':str(video),'source_seconds':length,'transcript_segments':len(spoken),
        'selection_method':'time_fallback_no_speech' if fallback else 'whisper_transcription_and_heuristics','clips':records}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--model',default='small')
    parser.add_argument('--min-seconds',type=int,default=30)
    parser.add_argument('--max-seconds',type=int,default=80)
    parser.add_argument('--max-clips',type=int,default=5)
    args=parser.parse_args()
    try:
        result=process(pathlib.Path(args.input),pathlib.Path(args.output),args.model,
            args.min_seconds,args.max_seconds,args.max_clips)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:sys.exit('Error: '+str(exc))