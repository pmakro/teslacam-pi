// Multi-camera player for one Tesla event: one-minute segments per camera, kept in sync.
// Single-camera view by default (the Pi Zero's wifi struggles with 6 parallel streams);
// "All" shows every camera in a grid.
(function () {
  var data = JSON.parse(document.getElementById('player-data').textContent);
  var segments = data.segments, cameras = data.cameras;
  var playerEl = document.getElementById('player'), views = document.getElementById('views');
  var playBtn = document.getElementById('play'), scrub = document.getElementById('scrub');
  var timeEl = document.getElementById('time'), camsEl = document.getElementById('cams');
  var rateEl = document.getElementById('rate'), info = document.getElementById('clipinfo');
  var timeline = document.getElementById('timeline');

  if (!segments.length) {
    views.innerHTML = '<div style="color:#aaa;padding:3rem;text-align:center">No video files in this event.</div>';
    document.getElementById('controls').style.display = 'none';
    return;
  }

  var LABELS = { front: 'Front', back: 'Rear', left_repeater: 'Left repeater', right_repeater: 'Right repeater',
                 left_pillar: 'Left pillar', right_pillar: 'Right pillar' };
  var durations = segments.map(function () { return 60; });
  var seg = 0, mode = 'single', selected = cameras.indexOf('front') >= 0 ? 'front' : cameras[0];
  var videos = [], playing = false, pendingSeek = null, seeking = false;

  function parse(iso) { return new Date(iso).getTime() / 1000; }
  var t0 = parse(segments[0].start);
  function offset(i) { var s = 0; for (var k = 0; k < i; k++) s += durations[k]; return s; }
  function total() { return offset(segments.length); }
  function fmt(s) { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }
  function master() { return videos[0]; }

  function shownCams() { return mode === 'grid' ? cameras : [selected]; }

  function build() {
    views.innerHTML = '';
    videos = [];
    playerEl.classList.toggle('grid', mode === 'grid');
    playerEl.classList.toggle('six', cameras.length > 4);
    shownCams().forEach(function (cam) {
      var wrap = document.createElement('div');
      wrap.className = 'view';
      var v = document.createElement('video');
      v.playsInline = true; v.muted = true; v.preload = 'auto';
      v.dataset.cam = cam;
      wrap.appendChild(v);
      var label = document.createElement('span');
      label.className = 'cam-label'; label.textContent = LABELS[cam] || cam;
      wrap.appendChild(label);
      if (mode === 'grid') {
        wrap.style.cursor = 'pointer';
        wrap.title = 'Show only this camera';
        wrap.addEventListener('click', function () { selectCam(cam); });
      }
      views.appendChild(wrap);
      videos.push(v);
    });
    var m = master();
    m.addEventListener('timeupdate', onTime);
    m.addEventListener('loadedmetadata', function () {
      if (isFinite(m.duration) && m.duration > 0) durations[seg] = m.duration;
      scrub.max = total();
      placeMarker();
      if (pendingSeek !== null) { setAll(pendingSeek); pendingSeek = null; }
    });
    m.addEventListener('ended', function () {
      if (seg < segments.length - 1) { load(seg + 1, 0, true); } else { setPlaying(false); }
    });
  }

  function load(i, t, autoplay) {
    seg = i;
    pendingSeek = t || 0;
    videos.forEach(function (v) {
      var url = segments[i].files[v.dataset.cam];
      if (url) { v.src = url; v.style.visibility = ''; } else { v.removeAttribute('src'); v.style.visibility = 'hidden'; }
      v.playbackRate = parseFloat(rateEl.value);
      v.load();
    });
    info.textContent = 'Clip ' + (i + 1) + ' of ' + segments.length + ' · starts ' + segments[i].start.replace('T', ' ');
    if (autoplay || playing) { playAll(); }
  }

  function setAll(t) {
    videos.forEach(function (v) { if (v.getAttribute('src')) { try { v.currentTime = t; } catch (e) {} } });
  }

  function playAll() {
    videos.forEach(function (v) { if (v.getAttribute('src')) { var p = v.play(); if (p && p.catch) p.catch(function () {}); } });
    setPlaying(true);
  }
  function pauseAll() { videos.forEach(function (v) { v.pause(); }); setPlaying(false); }
  function setPlaying(p) { playing = p; playBtn.textContent = p ? '❚❚ Pause' : '▶ Play'; }

  function onTime() {
    var m = master(), t = m.currentTime;
    if (!seeking) scrub.value = offset(seg) + t;
    timeEl.textContent = fmt(offset(seg) + t) + ' / ' + fmt(total());
    for (var k = 1; k < videos.length; k++) {
      var v = videos[k];
      if (!v.getAttribute('src')) continue;
      if (v.readyState > 1 && Math.abs(v.currentTime - t) > 0.3) v.currentTime = t;
      if (playing && !m.paused && v.paused && !v.ended) { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
    }
  }

  function seekTo(abs) {
    var i = 0;
    while (i < segments.length - 1 && abs >= offset(i + 1)) i++;
    var t = abs - offset(i);
    if (i !== seg) { load(i, t, playing); } else { setAll(t); }
  }

  function selectCam(cam) {
    var abs = offset(seg) + (master() ? master().currentTime : 0);
    if (cam === 'grid') { mode = 'grid'; } else { mode = 'single'; selected = cam; }
    renderCamButtons();
    build();
    var i = seg; seg = -1;
    load(i, abs - offset(i), playing);
  }

  function renderCamButtons() {
    camsEl.innerHTML = '';
    cameras.concat(cameras.length > 1 ? ['grid'] : []).forEach(function (cam) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'btn sm';
      b.textContent = cam === 'grid' ? 'All' : (LABELS[cam] || cam);
      if ((cam === 'grid' && mode === 'grid') || (mode === 'single' && cam === selected)) b.classList.add('active');
      b.addEventListener('click', function () { selectCam(cam); });
      camsEl.appendChild(b);
    });
  }

  var marker = null;
  function placeMarker() {
    if (!data.trigger) return;
    var at = parse(data.trigger) - t0;
    if (at < 0 || at > total()) return;
    if (!marker) { marker = document.createElement('div'); marker.className = 'marker'; marker.title = 'Trigger'; timeline.appendChild(marker); }
    marker.style.left = 'calc(' + (100 * at / total()) + '% - 1px)';
  }

  playBtn.addEventListener('click', function () { playing ? pauseAll() : playAll(); });
  scrub.addEventListener('input', function () { seeking = true; timeEl.textContent = fmt(scrub.value) + ' / ' + fmt(total()); });
  scrub.addEventListener('change', function () { seeking = false; seekTo(parseFloat(scrub.value)); });
  rateEl.addEventListener('change', function () { videos.forEach(function (v) { v.playbackRate = parseFloat(rateEl.value); }); });
  document.addEventListener('keydown', function (e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.code === 'Space') { e.preventDefault(); playing ? pauseAll() : playAll(); }
    if (e.code === 'ArrowRight') seekTo(offset(seg) + master().currentTime + 5);
    if (e.code === 'ArrowLeft') seekTo(Math.max(0, offset(seg) + master().currentTime - 5));
  });

  // Start a few seconds before the trigger.
  renderCamButtons();
  build();
  scrub.max = total();
  var start = data.trigger ? Math.max(0, parse(data.trigger) - t0 - 10) : 0;
  var first = 0;
  while (first < segments.length - 1 && start >= offset(first + 1)) first++;
  load(first, start - offset(first), false);
  placeMarker();
})();
