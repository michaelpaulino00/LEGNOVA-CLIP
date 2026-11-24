"""Face-aware vertical reframing of authorized source video.

Checks the whole minute for visible faces, estimates mouth movement where possible,
interpolates the crop smoothly, and zooms slightly to remove player chrome.
The active-speaker selection is visual/heuristic, not verified audio diarization.
"""
import json
import logging
import math
import subprocess

LOG = logging.getLogger("legnova-worker")


def _probe_size(video):
    data = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        text=True, timeout=25))
    stream = (data.get("streams") or [{}])[0]
    return int(stream["width"]), int(stream["height"])


def _edge_row_trim(frames, max_rows, right, from_bottom=False):
    """Find full-width white browser chrome attached to the outer image edge."""
    import numpy as np
    if frames is None or len(frames) == 0:
        return 0
    # Require broadly white, static bands in most samples, not bright scenery.
    rows = []
    for frame in frames[:min(8, len(frames))]:
        area = frame[:, :max(2, right), :]
        white = np.mean(np.min(area, axis=2) > 228, axis=1)
        rows.append(white)
    common = np.median(np.stack(rows), axis=0)
    if from_bottom:
        common = common[::-1]
    run = 0
    for score in common[:max_rows]:
        if score < .83:
            break
        run += 1
    return run if run >= 3 else 0


def _faces(gray, top, bottom, right, frontal, profile):
    import cv2
    roi = gray[top:bottom, :right]
    if roi.size == 0:
        return []
    detected = []
    for detector in (frontal, profile):
        if detector.empty():
            continue
        for x, y, fw, fh in detector.detectMultiScale(
                roi, scaleFactor=1.11, minNeighbors=2 if detector is frontal else 3,
                minSize=(14, 14)):
            detected.append((float(x), float(y + top), float(fw), float(fh)))
        if detector is profile:
            # OpenCV profile cascade has an orientation preference.
            mirrored = cv2.flip(roi, 1)
            for x,y,fw,fh in detector.detectMultiScale(
                    mirrored,scaleFactor=1.11,minNeighbors=3,minSize=(14,14)):
                detected.append((float(roi.shape[1]-x-fw),float(y+top),float(fw),float(fh)))
    # Avoid duplicates from the two Haar classifiers.
    detected.sort(key=lambda f: f[2]*f[3], reverse=True)
    distinct = []
    for f in detected:
        if not any(abs(f[0]+f[2]/2-(g[0]+g[2]/2)) < min(f[2],g[2])*.4
                   and abs(f[1]+f[3]/2-(g[1]+g[3]/2)) < min(f[3],g[3])*.4
                   for g in distinct):
            distinct.append(f)
    return distinct


def _lip_motion(first, second, face):
    """Approximate speaking by localized lower-face changes, not sound identity."""
    import numpy as np
    x,y,w,h = face
    x0=max(0,int(x+w*.24)); x1=min(first.shape[1],int(x+w*.76))
    y0=max(0,int(y+h*.59)); y1=min(first.shape[0],int(y+h*.89))
    # Eye region provides a rough control for general head/camera motion.
    e0=max(0,int(y+h*.21)); e1=min(first.shape[0],int(y+h*.46))
    if x1-x0<5 or y1-y0<5 or e1-e0<4:
        return 0.0
    mouth=float(np.mean(np.abs(first[y0:y1,x0:x1].astype(np.int16)
                                   -second[y0:y1,x0:x1].astype(np.int16))))
    eyes=float(np.mean(np.abs(first[e0:e1,x0:x1].astype(np.int16)
                                  -second[e0:e1,x0:x1].astype(np.int16))))
    return max(0.0, mouth-eyes*.65)


def smart_crop_filter(video, seconds=60):
    w,h = _probe_size(video)
    if w<200 or h<200:
        raise ValueError("Dimensiones de video inválidas")
    # Portrait sources can still include the browser's white top and bottom.
    if w/h < .95:
        return ("crop=iw:trunc(ih*0.90/2)*2:0:trunc(ih*0.05/2)*2,"
                "fps=24,scale=432:768:force_original_aspect_ratio=increase,"
                "crop=432:768,setsar=1")

    wide = w/h >= 1.38
    right = int(w*.80) if wide else w
    top = int(h*.09) if wide else 0
    bottom = int(h*.89) if wide else h
    centers = []
    face_samples = 0

    # Two nearby frames every four seconds reveal who is moving their mouth;
    # sampling across the WHOLE minute follows changes of camera/speaker.
    interval = 4
    try:
        import cv2
        import numpy as np
        sw = min(w, 512)
        sh = max(2, int(round(h*sw/w/2))*2)
        length = max(2, min(float(seconds), 60.0))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(video), "-t", str(length),
               "-vf", f"fps=2,select='lt(mod(n\\,8)\\,2)',scale={sw}:{sh}",
               "-vsync", "0", "-frames:v", "30", "-pix_fmt", "bgr24",
               "-f", "rawvideo", "pipe:1"]
        data = subprocess.run(cmd, capture_output=True, check=True, timeout=90).stdout
        frame_bytes = sw*sh*3
        count = min(30, len(data)//frame_bytes)
        if count >= 2:
            frames = np.frombuffer(data[:count*frame_bytes], np.uint8).reshape(
                count, sh, sw, 3)
            # Distinguish the moving player from the static right-side chat or
            # broad white webpage column. Browser layouts vary across screens.
            pair_motion = [np.abs(frames[j+1].astype(np.int16)-frames[j].astype(np.int16)).mean(axis=2)
                           for j in range(0,min(count-1,16),2)]
            if wide and pair_motion:
                motion = np.mean(np.stack(pair_motion),axis=0)
                col = motion[int(sh*.16):int(sh*.85)].mean(axis=0)
                col = np.convolve(col,np.ones(9)/9,mode='same')
                activity_floor = max(1.5,float(np.percentile(col[:max(2,int(sw*.32))],65))*.24)
                active = np.flatnonzero(col>activity_floor)
                if len(active)>int(sw*.07):
                    candidate = max(int(w*.29), min(int(w*.87), int((int(active[-1])+11)*w/sw)))
                    if candidate < right*.95:
                        right = candidate
                        LOG.info('Moving player detected; excluding static webpage after x=%s',right)
            player_right = max(2, min(sw, int(right*sw/w)))
            # White chrome is commonly taller than a fixed 8% crop.
            white_top = _edge_row_trim(frames, int(sh*.32), player_right)
            white_bottom = _edge_row_trim(frames, int(sh*.32), player_right, True)
            if white_top:
                top = max(top, int((white_top+2)*h/sh))
            if white_bottom:
                bottom = min(bottom, int((sh-white_bottom-2)*h/sh))
            right = max(2, min(w,right))
            rt = max(0, min(sh-2, int(top*sh/h)))
            rb = max(rt+2, min(sh,int(bottom*sh/h)))
            grays = [cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY) for fr in frames]
            frontal = cv2.CascadeClassifier(
                cv2.data.haarcascades+"haarcascade_frontalface_default.xml")
            profile = cv2.CascadeClassifier(
                cv2.data.haarcascades+"haarcascade_profileface.xml")
            # Compute geometric crop dimensions now, after white margins.
            region_h = bottom-top
            crop_h = max(2,(min(region_h,int(right*16/9))//2)*2)
            crop_w = max(2,(min(right,int(crop_h*9/16))//2)*2)
            current = max(0.0,(right-crop_w)*.5)
            for i in range(0,count,2):
                first,second = grays[i],grays[min(i+1,count-1)]
                detections = _faces(second,rt,rb,player_right,frontal,profile)
                best = None
                for face in detections:
                    x,y,fw,fh = face
                    cx = (x+fw/2)*w/sw
                    # Large foreground faces are preferred over an audience.
                    area = math.sqrt(fw*fh)
                    lips = _lip_motion(first,second,face)
                    closeness = max(0.0,1-abs(cx-(current+crop_w/2))/max(1,crop_w))
                    score = area*(1+min(1.1,lips/18.0))*(1+.25*closeness)
                    if best is None or score>best[0]:
                        best = (score,cx)
                if best:
                    desired = max(0.0,min(right-crop_w,best[1]-crop_w/2))
                    face_samples += 1
                else:
                    # Profile faces/microphones can defeat Haar. Use movement
                    # saliency around the performer instead of remaining fixed
                    # on the audience or the center of the browser window.
                    delta = np.abs(first.astype(np.int16)-second.astype(np.int16))
                    col_energy = delta[rt:rb,:player_right].mean(axis=0)
                    span = max(8,min(player_right,int(crop_w*sw/w)))
                    candidates = range(0,max(1,player_right-span+1),max(1,span//14))
                    strength,location = max(((float(col_energy[x:x+span].mean()),x)
                                             for x in candidates),default=(0.0,0))
                    desired = (max(0.0,min(right-crop_w,location*w/sw))
                               if strength>1.6 else current)
                # Faster on a scene change; smoother when following a face.
                shift = max(-crop_w*.72,min(crop_w*.72,desired-current))
                current = max(0.0,min(right-crop_w,current+shift*.85))
                centers.append(round(current/2)*2)
            LOG.info("Face tracking: %s samples, %s face matches; top=%s bottom=%s",
                     len(centers),face_samples,top,bottom)
    except Exception as exc:
        LOG.warning("Face sampling unavailable; using safe portrait crop: %s",
                    str(exc)[:180])

    right=max(2,min(w,right))
    bottom=min(h,max(top+2,bottom))
    region_h=bottom-top
    crop_h=max(2,(min(region_h,int(right*16/9))//2)*2)
    crop_w=max(2,(min(right,int(crop_h*9/16))//2)*2)
    y=max(0,min(h-crop_h,top+(region_h-crop_h)//2))//2*2
    fallback=round(max(0,(right-crop_w)//2)/2)*2
    # Interpolate camera position between observations; no sudden jumps.
    if len(centers)>1:
        expression=str(centers[-1])
        for i in range(len(centers)-2,-1,-1):
            start,end=centers[i],centers[i+1]
            ramp=f"({start}+({end-start})*max(0\\,min(1\\,(t-{i*interval})/{interval})))"
            expression=f"if(lt(t\\,{(i+1)*interval})\\,{ramp}\\,{expression})"
    else:
        expression=str(centers[0] if centers else fallback)

    # A small post-crop removes residual white player bars visible in prior
    # 432x768 clips (roughly 33px each) without adding a second encode pass.
    LOG.info("Portrait crop source=%sx%s area=%sx%s vertical=432x768 fps=24",
             w,h,crop_w,crop_h)
    return (f"setpts=PTS-STARTPTS,crop={crop_w}:{crop_h}:{expression}:{y},"
            "fps=24,scale=432:768,"
            "crop=432:680:0:44,scale=432:768,setsar=1")