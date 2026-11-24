"""Face-aware vertical crop of browser-recorded live video; never includes chat."""
import json
import logging
import subprocess

LOG=logging.getLogger('legnova-worker')

def smart_crop_filter(video, seconds=60):
    dimensions=json.loads(subprocess.check_output(
        ['ffprobe','-v','error','-select_streams','v:0','-show_entries',
         'stream=width,height','-of','json',str(video)],text=True,timeout=25))
    stream=(dimensions.get('streams') or [{}])[0]
    w,h=int(stream['width']),int(stream['height'])
    if w<200 or h<200:
        raise ValueError('Dimensiones de video inválidas')
    if w/h<0.95:
        return 'fps=18,scale=480:854:force_original_aspect_ratio=increase,crop=480:854,setsar=1'
    wide=w/h>=1.38
    # A YouTube watch page can contain chat on the right, title and comments.
    right=int(w*.79) if wide else w
    top=int(h*.08) if wide else 0
    bottom=int(h*.90) if wide else h
    sampled_faces=[]
    interval=8
    try:
        import cv2
        import numpy as np
        sw=min(w,640)
        sh=max(2,int(round(h*sw/w/2))*2)
        p=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
             '-i',str(video),'-t',str(min(seconds,60)),
             '-vf',f'fps=1/{interval},scale={sw}:{sh}',
             '-frames:v','8','-pix_fmt','bgr24','-f','rawvideo','pipe:1'],
             capture_output=True,check=True,timeout=75)
        frame_bytes=sw*sh*3
        n=min(8,len(p.stdout)//frame_bytes)
        if n:
            frames=np.frombuffer(p.stdout[:n*frame_bytes],np.uint8).reshape(n,sh,sw,3)
            grays=[cv2.cvtColor(f,cv2.COLOR_BGR2GRAY) for f in frames]
            motion=np.mean([cv2.absdiff(grays[i],grays[i-1]).astype(np.float32)
                            for i in range(1,n)],axis=0) if n>=3 else None
            if wide and motion is not None:
                col=motion.mean(axis=0)
                options=[]
                for cut in range(int(sw*.64),int(sw*.87),5):
                    left=float(col[:cut].mean());tail=float(col[cut:].mean())
                    if left>2.2 and tail<left*.55: options.append((left-tail,cut))
                if options:
                    right=min(right,int((max(options)[1]+8)*w/sw))
                row=motion[:,:max(1,int(right*sw/w))].mean(axis=1)
                threshold=max(2.0,float(np.percentile(row,70))*.32)
                active=np.flatnonzero(row>threshold)
                if len(active)>4 and active[-1]-active[0]>sh*.38:
                    top=max(0,int((active[0]-6)*h/sh))
                    bottom=min(h,int((active[-1]+7)*h/sh))
            roi_right=max(2,min(sw,int(right*sw/w)))
            roi_top=max(0,int(top*sh/h))
            roi_bottom=min(sh,max(roi_top+2,int(bottom*sh/h)))
            frontal=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
            profile=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_profileface.xml')
            for g in grays:
                roi=g[roi_top:roi_bottom,:roi_right]
                faces=[]
                for model in (frontal,profile):
                    if not model.empty() and roi.size:
                        for x,y,fw,fh in model.detectMultiScale(
                            roi,scaleFactor=1.13,minNeighbors=4,minSize=(24,24)):
                            faces.append(((x+fw/2)*w/sw,float(fw*fh)))
                sampled_faces.append(faces)
    except Exception as exc:
        LOG.warning('Detector de encuadre no disponible; recorte seguro del reproductor: %s',str(exc)[:160])
    right=max(2,min(w,right))
    bottom=min(h,max(top+2,bottom))
    region_h=bottom-top
    crop_h=min(region_h,int(right*16/9))//2*2
    crop_w=min(right,int(crop_h*9/16))//2*2
    crop_w=max(2,crop_w);crop_h=max(2,crop_h)
    y=max(0,min(h-crop_h,top+(region_h-crop_h)//2))//2*2
    fallback=max(0,(right-crop_w)//2)
    previous=float(fallback)
    xs=[]
    for faces in sampled_faces:
        if faces:
            ranked=sorted(faces,key=lambda q:q[1]/(1+abs(q[0]-(previous+crop_w/2))/max(1,crop_w)),reverse=True)
            center=ranked[0][0]
            nearby=[v for v in ranked if abs(v[0]-center)<crop_w*.54]
            center=sum(x*a for x,a in nearby)/sum(a for _,a in nearby)
            desired=max(0,min(right-crop_w,center-crop_w/2))
            desired=max(previous-crop_w*.38,min(previous+crop_w*.38,desired))
            previous=.38*previous+.62*desired
        xs.append(round(previous/2)*2)
    if len(xs)>1:
        expr=str(xs[-1])
        for i in range(len(xs)-2,-1,-1):
            a,b=xs[i],xs[i+1]
            ramp=f'({a}+({b-a})*max(0\\,min(1\\,(t-{i*interval})/{interval})))'
            expr=f'if(lt(t\\,{(i+1)*interval})\\,{ramp}\\,{expr})'
    else:
        expr=str(xs[0] if xs else fallback)
    LOG.info('Intelligent portrait crop: source=%sx%s player-right=%s top=%s bottom=%s faces=%s',
             w,h,right,top,bottom,sum(bool(f) for f in sampled_faces))
    return (f'setpts=PTS-STARTPTS,crop={crop_w}:{crop_h}:{expr}:{y},'
            'fps=18,scale=480:854,setsar=1')
