(function () {
  'use strict';

  var feedEl = document.getElementById('feed');
  var inputEl = document.getElementById('input');
  var sendBtn = document.getElementById('send');
  var captionEl = document.getElementById('caption');
  var statusEl = document.getElementById('status');
  var resetBtn = document.getElementById('reset');
  var quickEl = document.getElementById('quick');
  var avatarEl = document.querySelector('.avatar');
  var soundBtn = document.getElementById('sound');
  var voiceSel = document.getElementById('voice');

  var statFirst = document.getElementById('stat-first');
  var statTotal = document.getElementById('stat-total');
  var statRound = document.getElementById('stat-round');
  var statChars = document.getElementById('stat-chars');
  var statAudio = document.getElementById('stat-audio');

  var VIEWERS = ['小鱼干', '老张', '夜猫子', '阿杰', '不吃香菜', '柠檬茶', '半糖', '路人甲', '晚风', '大聪明'];
  var QUICK = ['你是AI吧', '送了个跑车', '这个多少钱', '主播唱个歌', '大肥鱼你几斤'];

  var busy = false;
  var round = 0;
  var viewerCursor = 0;

  var soundOn = true;
  var voiceId = localStorage.getItem('fish.voice') || '';
  var voiceMeta = {};

  // ---- 音频输出 ----
  // 所有语音进同一个队列，严格按「第几句」播放。
  // 之前是请求并发、谁先拿到响应谁先排，两段音频的块会在同一条时间轴上交错，
  // 听起来就是读到一半顺序乱了。现在按序号占位，缺号就等，绝不跳播。
  var audioCtx = null;
  var playClock = 0;
  var slots = new Map(); // seq -> {buf, turn, text} | null（null = 这段失败了，占位跳过）
  var playSeq = 0; // 下一个该排程的序号
  var putSeq = 0; // 下一个要入队的序号
  var speakTrack = null; // 正在出声的那一轮，逐字跟读以它为准

  function getCtx() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return audioCtx;
  }

  function nextViewer() {
    var name = VIEWERS[viewerCursor % VIEWERS.length];
    viewerCursor += 1;
    return name;
  }

  function clearEmpty() {
    var placeholder = feedEl.querySelector('.empty');
    if (placeholder) placeholder.remove();
  }

  function stickToBottom() {
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  function addAudience(who, text) {
    clearEmpty();
    var row = document.createElement('div');
    row.className = 'msg msg-audience';
    var name = document.createElement('span');
    name.className = 'who';
    name.textContent = who + '：';
    row.appendChild(name);
    row.appendChild(document.createTextNode(text));
    feedEl.appendChild(row);
    stickToBottom();
  }

  function addAiBubble() {
    clearEmpty();
    var row = document.createElement('div');
    row.className = 'msg msg-ai';

    var head = document.createElement('div');
    head.className = 'ai-head';

    var name = document.createElement('span');
    name.className = 'ai-name';
    name.textContent = '大肥鱼';

    var meta = document.createElement('span');
    meta.className = 'ai-meta';
    meta.textContent = '思考中…';

    // 这一句的语气指示（DeepSeek 给的，直接喂给 MiMo 当风格提示）
    var tag = document.createElement('span');
    tag.className = 'ai-style';
    tag.style.display = 'none';

    head.appendChild(name);
    head.appendChild(meta);

    var body = document.createElement('div');
    body.className = 'ai-body waiting';
    body.textContent = '大肥鱼正在接收脑电波……';

    row.appendChild(head);
    row.appendChild(tag);
    row.appendChild(body);
    feedEl.appendChild(row);
    stickToBottom();

    return { row: row, body: body, meta: meta, tag: tag };
  }

  // 语音真正开口的这一刻才把对应文字放出来。
  // 反过来做（字先出来、两秒后才有声音）观众会先读完再听到，嘴和字完全对不上。
  function revealText(turn) {
    if (turn.bubble.body.classList.contains('waiting')) {
      turn.bubble.body.classList.remove('waiting');
      turn.bubble.meta.textContent = '说话中…';
    }
    turn.bubble.body.textContent = turn.shown;
    captionEl.textContent = turn.shown;
    stickToBottom();
  }

  function setBusy(on) {
    busy = on;
    inputEl.disabled = on;
    sendBtn.disabled = on;
    statusEl.textContent = on ? '说话中' : '待机';
    statusEl.classList.toggle('busy', on);
    captionEl.classList.toggle('typing', on);
    if (!on) inputEl.focus();
  }

  // 把流式文本切成适合朗读的小句：句号必切，逗号攒够 9 个字再切。
  // 切得越早，第一声出得越快——这是整个语音链路里最有效的优化。
  // 但太短不行：「鱼。」这种两字一句念出来又断又怪，所以设一个下限。
  var MIN_SEG = 7;

  function takeSegments(buf, force) {
    var list = [];
    var start = 0;
    var hard = '。！？!?…\n';
    var soft = '，,；;、';
    for (var i = 0; i < buf.length; i += 1) {
      var ch = buf.charAt(i);
      var span = i - start + 1;
      var cut = false;
      if (hard.indexOf(ch) >= 0 && span >= MIN_SEG) cut = true;
      else if (soft.indexOf(ch) >= 0 && span >= 9) cut = true;
      if (cut) {
        list.push(buf.slice(start, i + 1));
        start = i + 1;
      }
    }
    var rest = buf.slice(start);
    if (force && rest.trim()) {
      list.push(rest);
      rest = '';
    }
    return { list: list, rest: rest };
  }

  function noteEngine(turn, used) {
    if (!used || turn.engineNote) return;
    var want = voiceMeta[voiceId] && voiceMeta[voiceId].engine;
    var got = voiceMeta[used] && voiceMeta[used].engine;
    if (want === 'mimo' && got === 'say') turn.engineNote = '已降级为系统语音';
    else turn.engineNote = got === 'mimo' ? 'MiMo' : '系统语音';
  }

  // 声音真正出来时才更新指标、摘掉等待态；文字交给 tickSpeech 逐字推
  function onAudioStart(turn, delay, spokenText) {
    setTimeout(function () {
      if (!turn.audioStarted) {
        turn.audioStarted = true;
        turn.audioDelay = Math.round(performance.now() - turn.t0) + 'ms';
        statAudio.textContent = turn.audioDelay;
        if (turn.bubble.body.classList.contains('waiting')) {
          turn.bubble.body.classList.remove('waiting');
          turn.bubble.meta.textContent = '说话中…';
        }
      }
      speakTrack = turn; // 从这一刻起，字幕跟着播放进度走
      if (soundOn) statusEl.textContent = '出声中';
      avatarEl.classList.add('speaking');
    }, delay);
  }

  // 把一段 PCM 收完再拼成 AudioBuffer。
  // 有意不边收边播：流式块如果直接写 playClock，两段并行时时间轴就会交错。
  // MiMo 首块之后剩下的分片几乎瞬间到齐，等收完只多几十毫秒，换来顺序绝对正确。
  function collectPcm(body, rate) {
    var ctx = getCtx();
    if (!ctx) return Promise.reject(new Error('浏览器不支持 Web Audio'));
    var reader = body.getReader();
    var pieces = [];
    var total = 0;

    function step() {
      return reader.read().then(function (r) {
        if (r.done) return null;
        pieces.push(r.value);
        total += r.value.length;
        return step();
      });
    }

    return step().then(function () {
      var usable = total - (total % 2); // PCM16 一个采样两字节，奇数字节丢掉
      if (usable <= 0) throw new Error('没有音频数据');
      var merged = new Uint8Array(usable);
      var off = 0;
      for (var i = 0; i < pieces.length && off < usable; i += 1) {
        var p = pieces[i];
        var take = Math.min(p.length, usable - off);
        merged.set(take === p.length ? p : p.subarray(0, take), off);
        off += take;
      }
      var count = usable / 2;
      var view = new DataView(merged.buffer);
      var buffer = ctx.createBuffer(1, count, rate || 24000);
      var ch = buffer.getChannelData(0);
      for (var j = 0; j < count; j += 1) ch[j] = view.getInt16(j * 2, true) / 32768;
      return buffer;
    });
  }

  function decodeWav(buf) {
    var ctx = getCtx();
    if (!ctx) return Promise.reject(new Error('浏览器不支持 Web Audio'));
    return ctx.decodeAudioData(buf.slice(0));
  }

  // 中文拿不到音素时间戳，只能用「音频时长 ÷ 字数」反推。
  // 但等分不准：逗号要停、句末拖音更长。所以给每个字一个权重，
  // 让文字跑的速度大致贴着声音的节奏。
  function charWeights(text) {
    var w = [];
    var total = 0;
    for (var i = 0; i < text.length; i += 1) {
      var ch = text.charAt(i);
      var wt = 1;
      if ('。！？!?…'.indexOf(ch) >= 0) wt = 2.2;
      else if ('，,；;、：:'.indexOf(ch) >= 0) wt = 1.5;
      else if (ch === ' ' || ch === '\n') wt = 0.4;
      w.push(wt);
      total += wt;
    }
    return { w: w, total: total };
  }

  // 逐字跟读：每一帧看一眼播到哪儿，反推该显示到第几个字。
  // 只在「字数变了」时才动 DOM——中文一秒才五六个字，这点开销可以忽略。
  function tickSpeech() {
    requestAnimationFrame(tickSpeech);
    if (!speakTrack || !audioCtx) return;

    var turn = speakTrack;
    var segs = turn.segs;
    if (!segs.length) return;

    var now = audioCtx.currentTime;
    var idx = -1;
    for (var i = segs.length - 1; i >= 0; i -= 1) {
      if (now >= segs[i].startAt) {
        idx = i;
        break;
      }
    }
    if (idx < 0) return; // 还没起播

    var seg = segs[idx];
    var p = seg.dur > 0 ? (now - seg.startAt) / seg.dur : 1;
    if (p < 0) p = 0;
    if (p > 1) p = 1;

    var target = seg.weights.total * p;
    var acc = 0;
    var k = 0;
    for (; k < seg.weights.w.length; k += 1) {
      acc += seg.weights.w[k];
      if (acc >= target) break;
    }

    var prefix = '';
    for (var j = 0; j < idx; j += 1) prefix += segs[j].text;
    var shown = prefix + seg.text.slice(0, k);

    // 只准往前长，绝不回退：音频时钟和帧时间有细微抖动，回退看着像卡带
    if (shown === turn.shown || shown.length < turn.shown.length) return;

    turn.shown = shown;
    revealText(turn);
  }

  // 只有这里会改 playClock，而且只被 flushSlots 按序号依次调用，
  // 所以时间轴一定单调递增，不会出现「后一句插到前一句中间」。
  function schedule(item) {
    var ctx = getCtx();
    if (!ctx) return;
    var now = ctx.currentTime;
    var startAt = Math.max(now + 0.04, playClock);
    var src = ctx.createBufferSource();
    src.buffer = item.buf;
    src.connect(ctx.destination);
    src.start(startAt);
    playClock = startAt + item.buf.duration;

    // 记下这段的起止和「每个字大概占多少时间」，逐字跟读要用
    item.startAt = startAt;
    item.dur = item.buf.duration;
    item.weights = charWeights(item.text);
    item.prefixLen = item.turn.segs.reduce(function (n, s) {
      return n + s.text.length;
    }, 0);
    item.turn.segs.push(item);

    onAudioStart(item.turn, Math.max(0, (startAt - now) * 1000), item.text);
  }

  function flushSlots() {
    while (slots.has(playSeq)) {
      var item = slots.get(playSeq);
      slots.delete(playSeq);
      playSeq += 1;
      if (item) schedule(item);
    }
  }

  function enqueue(seq, item) {
    slots.set(seq, item);
    flushSlots();
  }

  function withTimeout(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        reject(new Error(label + '超时'));
      }, ms);
      promise.then(
        function (v) {
          clearTimeout(timer);
          resolve(v);
        },
        function (e) {
          clearTimeout(timer);
          reject(e);
        }
      );
    });
  }

  // 只负责「文字变成 AudioBuffer」，不碰播放时间轴。
  // 把下载和排程分开，才能做到「请求并发、播放保序」。
  function fetchJob(turn, text, useStream, allowRetry) {
    return fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: text,
        voice: voiceId,
        style: turn.style || '',
        stream: useStream ? 1 : 0
      })
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('语音合成失败 ' + resp.status);
        noteEngine(turn, resp.headers.get('X-TTS-Voice'));
        if (useStream) {
          var rate = parseInt(resp.headers.get('X-PCM-Rate') || '24000', 10) || 24000;
          return collectPcm(resp.body, rate);
        }
        return resp.arrayBuffer().then(decodeWav);
      })
      .catch(function (err) {
        // 流式在吐字节前挂了（503）或中途断了：退一次非流式，
        // 服务端那边会自动降级成系统语音，至少还有声音。
        if (useStream && allowRetry !== false) return fetchJob(turn, text, false, false);
        throw err;
      });
  }

  function speak(turn, raw) {
    var text = (raw || '').trim();
    if (!text || !soundOn) return;
    // 只有标点没有字（比如多切出来一个「。」）就别浪费一次合成了
    if (text.replace(/[。！？!?…，,、；;\s]/g, '').length === 0) return;

    var seq = putSeq;
    putSeq += 1;
    turn.pending += 1;

    var conf = voiceMeta[voiceId];
    withTimeout(fetchJob(turn, text, !!(conf && conf.stream)), 15000, '语音合成')
      .then(function (buf) {
        enqueue(seq, { buf: buf, turn: turn, text: text });
      })
      .catch(function () {
        // 必须占住这个号，否则后面的句子会一直等一个永远不会来的 seq
        enqueue(seq, null);
      })
      .then(function () {
        turn.pending -= 1;
      });
  }

  // 等这一轮所有语音都排进时间轴并播完。
  // playClock 就是「最后一段响完的时刻」，比逐个挂 onended 简单也更准。
  function waitAudio(turn) {
    return new Promise(function (resolve) {
      function check() {
        if (turn.pending > 0) {
          setTimeout(check, 40);
          return;
        }
        var remain = audioCtx ? (playClock - audioCtx.currentTime) * 1000 : 0;
        if (remain > 120) {
          setTimeout(check, remain + 60);
          return;
        }
        resolve();
      }
      check();
    });
  }

  function updateBubbleMeta(turn) {
    // 还没开口时先把「思考中…」留着，别让一行指标把等待态盖掉；
    // 真正出声后 revealText 之后会再调一次，指标照样能补上。
    if (turn.bubble.body.classList.contains('waiting')) return;

    var parts = [];
    if (typeof turn.firstMs === 'number') {
      parts.push('首字 ' + turn.firstMs + 'ms');
      parts.push('共 ' + turn.totalMs + 'ms');
    }
    if (turn.engineNote) parts.push(turn.engineNote);
    if (turn.audioDelay) parts.push('起播 ' + turn.audioDelay);
    if (parts.length) turn.bubble.meta.textContent = parts.join(' · ');
  }

  // 只取第一句，剩下的留给后面。用于「首句先行」：
  // MiMo 每次请求有 2 秒多的前置开销，早一点发出去就能早一点出声。
  function firstSentence(buf) {
    for (var i = 0; i < buf.length; i += 1) {
      if ('。！？!?…\n'.indexOf(buf.charAt(i)) >= 0 && i + 1 >= MIN_SEG) {
        return { text: buf.slice(0, i + 1), rest: buf.slice(i + 1) };
      }
    }
    return null;
  }

  function handleEvent(evt, turn) {
    if (evt.type === 'style') {
      // 语气指示比台词先到，前端只把它当参数，不上屏
      turn.style = evt.text || '';
      if (turn.bubble.tag && turn.style) {
        turn.bubble.tag.textContent = '语气 · ' + turn.style;
        turn.bubble.tag.style.display = '';
        stickToBottom();
      }
      return;
    }
    if (evt.type === 'delta') {
      turn.answer += evt.text;

      var meta = voiceMeta[voiceId];
      var segment = meta ? meta.segment : true;
      var streaming = meta ? meta.stream : false;

      if (!soundOn) {
        // 不出声就直接出字，没有「等声音」这回事
        turn.shown = turn.answer;
        revealText(turn);
      }

      if (segment && soundOn) {
        turn.speakBuf += evt.text;
        var parts = takeSegments(turn.speakBuf, false);
        turn.speakBuf = parts.rest;
        for (var i = 0; i < parts.list.length; i += 1) speak(turn, parts.list[i]);
      } else if (!segment && streaming && soundOn) {
        // 首句先行：第一句一凑够就先送去合成，不必等整段生成完
        turn.speakBuf += evt.text;
        if (!turn.firstSent) {
          var head = firstSentence(turn.speakBuf);
          if (head) {
            turn.firstSent = true;
            turn.speakBuf = head.rest;
            speak(turn, head.text);
          }
        }
      }
      stickToBottom();
      return;
    }
    if (evt.type === 'done') {
      turn.done = evt;
      return;
    }
    if (evt.type === 'error') {
      throw new Error(evt.message || '模型调用失败');
    }
  }

  async function send(rawText) {
    var content = (rawText || '').trim();
    if (busy || !content) return;

    if (soundOn) getCtx();
    setBusy(true);
    addAudience(nextViewer(), content);
    inputEl.value = '';
    captionEl.textContent = '';
    avatarEl.classList.add('speaking');

    var turn = {
      answer: '',
      shown: '', // 已经「说出口」的文字，和声音同步吐出
      style: '', // 这一句的语气指示，随每段语音一起发给合成
      speakBuf: '',
      segs: [], // 已排进时间轴的语音段，逐字跟读用
      pending: 0,
      audioStarted: false,
      t0: performance.now(),
      done: null,
      bubble: addAiBubble()
    };

    try {
      var resp = await fetch('/api/danmaku', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: content })
      });
      if (!resp.ok || !resp.body) throw new Error('服务返回 ' + resp.status);

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;

        buffer += decoder.decode(chunk.value, { stream: true });
        var frames = buffer.split('\n\n');
        buffer = frames.pop();

        for (var i = 0; i < frames.length; i += 1) {
          var payload = null;
          var lines = frames[i].split('\n');
          for (var j = 0; j < lines.length; j += 1) {
            if (lines[j].indexOf('data:') === 0) {
              payload = lines[j].slice(5).trim();
              break;
            }
          }
          if (!payload) continue;
          var evt;
          try {
            evt = JSON.parse(payload);
          } catch (err) {
            continue;
          }
          handleEvent(evt, turn);
        }
      }

      if (!turn.answer) throw new Error('模型没有返回内容');

      // 最后半句（没有标点结尾）也要念出来；
      // 不切句的音色（MiMo）则整段一次合成，省掉重复的固定开销。
      var metaConf = voiceMeta[voiceId];
      var segmented = metaConf ? metaConf.segment : true;
      var tail = turn.speakBuf;
      turn.speakBuf = '';
      if (segmented) {
        if (tail.trim()) speak(turn, tail);
      } else if (tail.trim()) {
        speak(turn, tail); // 首句已经先发了，剩下的整段说完
      } else if (!turn.firstSent) {
        speak(turn, turn.answer); // 一句都还没发（没有可切的句号），整段来
      }

      var info = turn.done || {};
      var firstMs = info.first_ms || 0;
      var totalMs = info.total_ms || 0;

      turn.firstMs = firstMs;
      turn.totalMs = totalMs;
      updateBubbleMeta(turn);
      statFirst.textContent = firstMs + 'ms';
      statTotal.textContent = totalMs + 'ms';
      statChars.textContent = info.chars || turn.answer.length;
      round += 1;
      statRound.textContent = round;

      // 等这一轮的声音全部播完再放开输入，避免两条弹幕的回复叠在一起
      if (soundOn) {
        if (!turn.audioStarted) statusEl.textContent = '合成语音…';
        await waitAudio(turn);
        // 某段合成失败、或被跳过时，文字可能没吐全；
        // 一句都没出声的话气泡还停在等待态，这里统一兜底，别让观众一直看「接收脑电波」
        if (turn.shown !== turn.answer || turn.bubble.body.classList.contains('waiting')) {
          turn.shown = turn.answer;
          revealText(turn);
        }
        // 到这才知道实际用的是哪个引擎（可能已降级），补写一次
        updateBubbleMeta(turn);
      }
    } catch (err) {
      turn.bubble.row.classList.add('failed');
      turn.bubble.body.classList.remove('waiting');
      turn.bubble.meta.textContent = '失败';
      turn.bubble.body.textContent = err.message || '请求失败';
      captionEl.textContent = '（这一句没说出来）';
    } finally {
      speakTrack = null; // 这一轮说完了，停掉逐字推进
      avatarEl.classList.remove('speaking');
      setBusy(false);
    }
  }

  function loadVoices() {
    fetch('/api/voices')
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var list = data.voices || [];
        voiceSel.innerHTML = '';
        voiceMeta = {};
        list.forEach(function (v) {
          voiceMeta[v.id] = v;
          var opt = document.createElement('option');
          opt.value = v.id;
          opt.textContent = v.label;
          voiceSel.appendChild(opt);
        });
        if (!voiceId) voiceId = data.default || (list[0] && list[0].id) || '';
        voiceSel.value = voiceId;
        if (voiceSel.value !== voiceId) {
          voiceId = data.default || '';
          voiceSel.value = voiceId;
        }
      })
      .catch(function () {
        voiceSel.innerHTML = '<option value="">语音不可用</option>';
      });
  }

  QUICK.forEach(function (text) {
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = text;
    chip.addEventListener('click', function () {
      send(text);
    });
    quickEl.appendChild(chip);
  });

  sendBtn.addEventListener('click', function () {
    send(inputEl.value);
  });

  inputEl.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      send(inputEl.value);
    }
  });

  soundBtn.addEventListener('click', function () {
    soundOn = !soundOn;
    soundBtn.textContent = soundOn ? '声音 开' : '声音 关';
    soundBtn.classList.toggle('on', soundOn);
    if (!soundOn && audioCtx) {
      playClock = audioCtx.currentTime;
    } else if (soundOn) {
      getCtx();
    }
  });

  voiceSel.addEventListener('change', function () {
    voiceId = voiceSel.value;
    localStorage.setItem('fish.voice', voiceId);
  });

  resetBtn.addEventListener('click', async function () {
    if (busy) return;
    try {
      await fetch('/api/reset', { method: 'POST' });
    } catch (err) {
      /* 服务没起来也不影响清屏 */
    }
    feedEl.innerHTML = '';
    var placeholder = document.createElement('div');
    placeholder.className = 'empty';
    placeholder.textContent = '上下文已清空。继续输入弹幕吧。';
    feedEl.appendChild(placeholder);
    captionEl.textContent = '等你说句话';
    statFirst.textContent = '—';
    statTotal.textContent = '—';
    statChars.textContent = '0';
    statAudio.textContent = '—';
    round = 0;
    statRound.textContent = '0';
  });

  loadVoices();
  soundBtn.classList.add('on');
  setBusy(false);
  requestAnimationFrame(tickSpeech); // 逐字跟读的驱动
})();
