"""Face-aware vertical reframing of authorized source video.

Checks the whole minute for visible faces, estimates mouth movement where possible,
locks a fixed crop to the dominant visual speaker, and removes player chrome.
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


def _weighted_median(values):
    """Stable representative position; transient detections cannot pull the crop."""
    ordered = sorted(values, key=lambda item: item[0])
    halfway = sum(weight for _, weight in ordered) / 2.0
    total = 0.0
    for value, weight in ordered:
        total += weight
        if total >= halfway:
            return value
    return ordered[-1][0]


def _speaker_lock(observations, crop_width, player_width):
    """Associate nearby faces, then lock to one dominant visual speaker per clip.

    Lip motion is only a visual proxy for speech, not audio diarization.
    All coordinates are in the sampled frame. No per-frame pan or saliency chase.
    """
    tracks = []
    for index, detections in enumerate(observations):
        # Match each observation to at most one existing track.
        # Spatial continuity, scale and vertical position discourage face swaps.
        candidates = []
        for di, (cx, cy, fw, fh, lips) in enumerate(detections):
            for ti, track in enumerate(tracks):
                last_index, px, py, pw, ph, _ = track[-1]
                gap = index - last_index
                if not 1 <= gap <= 3:
                    continue
                dx, dy = abs(cx-px), abs(cy-py)
                xgate = max(crop_width*.15, (fw+pw)*.42)
                ygate = max(crop_width*.12, (fh+ph)*.40)
                scale = abs(math.log(max(1.0, fw*fh)/max(1.0, pw*ph)))
                if dx <= xgate and dy <= ygate and scale <= 1.25:
                    candidates.append((dx/xgate + dy/ygate + .35*scale + .10*(gap-1), di, ti))
        used_faces, used_tracks = set(), set()
        for _, di, ti in sorted(candidates):
            if di in used_faces or ti in used_tracks:
                continue
            tracks[ti].append((index, *detections[di]))
            used_faces.add(di)
            used_tracks.add(ti)
        for di, detection in enumerate(detections):
            if di not in used_faces:
                tracks.append([(index, *detection)])
    if not tracks:
        return None, 0, 0

    def speech(lips):
        # Cap motion spikes caused by cuts, gestures and camera movement.
        return min(2.5, max(0.0, lips-1.5)/5.0)

    def weight(item):
        _, cx, _, fw, fh, lips = item
        prominence = min(1.2, max(.35, math.sqrt(fw*fh)/max(1.0, crop_width*.36)))
        return (.6 + speech(lips)) * prominence

    # Prefer repeated visibility and sustained speech evidence rather than
    # a single large or moving face. A lone detection is a fallback only.
    repeated = [track for track in tracks if len(track) >= 2]
    eligible = repeated or tracks
    def rank(track):
        evidence = sum(weight(item) for item in track)
        visible = len(track)
        speaking = sum(speech(item[-1]) >= .45 for item in track)
        return evidence + .25*visible + .40*speaking
    primary = max(eligible, key=rank)
    center = _weighted_median([(item[1], weight(item)) for item in primary])
    center = max(0.0, min(float(player_width), center))
    return center, len(primary), len(tracks)


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
    locked_center = None

    # Two nearby frames every four seconds reveal who is moving their mouth;
    # sampling across the WHOLE minute identifies a stable protagonist.
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
            observations = []
            for i in range(0,count,2):
                first,second = grays[i],grays[min(i+1,count-1)]
                detections = _faces(second,rt,rb,player_right,frontal,profile)
                observations.append([
                    (x+fw/2, y+fh/2, fw, fh, _lip_motion(first,second,face))
                    for face in detections
                    for x,y,fw,fh in [face]
                ])
            lock, matches, track_count = _speaker_lock(
                observations, crop_w*sw/w, player_right)
            if lock is not None:
                locked_center = lock*w/sw
            LOG.info("Speaker lock: %s samples, %s matched protagonist, %s tracks; center=%s top=%s bottom=%s",
                     len(observations),matches,track_count,
                     round(locked_center,1) if locked_center is not None else "fallback",top,bottom)
    except Exception as exc:
        LOG.warning("Face sampling unavailable; using safe portrait crop: %s",
                    str(exc)[:180])

    right=max(2,min(w,right))
    bottom=min(h,max(top+2,bottom))
    region_h=bottom-top
    crop_h=max(2,(min(region_h,int(right*16/9))//2)*2)
    crop_w=max(2,(min(right,int(crop_h*9/16))//2)*2)
    y=max(0,min(h-crop_h,top+(region_h-crop_h)//2))//2*2
    # One stable horizontal framing for the entire clip. No interpolation,
    # no jumping between faces, and no chase of unrelated movement.
    center = locked_center if locked_center is not None else right/2
    expression = str(int(max(0,min(right-crop_w,center-crop_w/2)))//2*2)

    # A small post-crop removes residual white player bars visible in prior
    # 432x768 clips (roughly 33px each) without adding a second encode pass.
    LOG.info("Portrait crop source=%sx%s area=%sx%s vertical=432x768 fps=24",
             w,h,crop_w,crop_h)
    return (f"setpts=PTS-STARTPTS,crop={crop_w}:{crop_h}:{expression}:{y},"
            "fps=24,scale=432:768,"
            "crop=432:680:0:44,scale=432:768,setsar=1")