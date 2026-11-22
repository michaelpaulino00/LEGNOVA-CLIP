#!/usr/bin/env python3
"""Isolated low-memory Whisper process. Emits transcript JSON on stdout only."""
import json
import os
import sys

def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: transcribe_audio.py VIDEO MODEL')
    from faster_whisper import WhisperModel
    video, model_name=sys.argv[1:3]
    model=WhisperModel(model_name,device='cpu',compute_type='int8',cpu_threads=1,num_workers=1)
    segments, _=model.transcribe(video,language=None,vad_filter=True,
        beam_size=1,best_of=1,condition_on_previous_text=False)
    records=[{'start':float(s.start),'end':float(s.end),'text':s.text.strip()}
             for s in segments if s.text.strip() and s.end>s.start]
    print(json.dumps(records,ensure_ascii=False))
if __name__=='__main__':main()