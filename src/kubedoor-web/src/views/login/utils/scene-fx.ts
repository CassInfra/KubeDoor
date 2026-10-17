/**
 * 登录页背景动效(Canvas 2D):
 * - 月洞门:门洞内沿的呼吸光、门环上流动的光点、外圈缓慢转动的刻度光弧、向外扩散的圆波
 * - 湖面:随机泛起的水波,鼠标划过 / 点击湖面、花瓣落水时也会泛起
 * - 梅枝上飘落的花瓣、莲花升起的萤光
 *
 * 坐标都是背景图在 1920x1080 下的像素位置,绘制时按背景 cover 后的实际大小整体缩放,
 * 换背景图时只需要改下面这几组坐标。
 */

export interface SceneFx {
  /** 视口或背景尺寸变化后重新计算画布 */
  resize(): void;
  /** 鼠标 / 触摸:湖面上泛起水波,其它地方是一个小光圈 */
  pointer(clientX: number, clientY: number, down: boolean): void;
  /** 登录成功:月洞门连续发出几道圆波 */
  burst(): void;
  setDark(dark: boolean): void;
  destroy(): void;
}

/** 背景图的参考宽度:下面所有坐标都是背景在这个宽度下的像素位置 */
export const REF_W = 1920;

/** 月洞门圆心与门环内外半径 */
const GATE = { x: 1241, y: 543, inner: 234, outer: 304 };
/** 门环露出地面的部分(画布角度,顺时针):左下 135° 经过顶部到右下 43° */
const ARC_FROM = (135 / 180) * Math.PI;
const ARC_TO = ((360 + 43) / 180) * Math.PI;
/** 湖面范围,以及不出水波的地方(两朵莲花) */
const WATER = { x0: 560, x1: 1900, y0: 800, y1: 1066 };
const WATER_SKIP = [
  { x0: 1360, x1: 1640, y0: 715, y1: 865 },
  { x0: 790, x1: 905, y0: 755, y1: 825 }
];
/** 梅花瓣飘落的起点(右上角的梅枝) */
const PLUM = { x0: 1340, x1: 1870, y0: 60, y1: 340 };
/** 萤光从莲花、花苞和门下的光点升起,w 是权重 */
const MOTE_SOURCES = [
  { x: 1488, y: 752, sx: 60, sy: 26, w: 5 },
  { x: 845, y: 786, sx: 26, sy: 12, w: 2 },
  { x: 230, y: 922, sx: 10, sy: 14, w: 1 },
  { x: 1766, y: 998, sx: 10, sy: 10, w: 1 },
  { x: 1098, y: 782, sx: 8, sy: 5, w: 1 }
];
const MOTE_WEIGHT = MOTE_SOURCES.reduce((sum, s) => sum + s.w, 0);
/** 门环外两圈慢慢转动的刻度光弧,segs 是各段的起止角度(弧度) */
const HUD = [
  {
    r: GATE.outer + 16,
    speed: 0.05,
    width: 1.4,
    alpha: 0.55,
    dash: [] as number[],
    segs: [
      [0.1, 0.9],
      [1.25, 1.7],
      [2.2, 3.3],
      [3.85, 4.35],
      [4.75, 5.8]
    ]
  },
  {
    r: GATE.outer + 30,
    speed: -0.035,
    width: 1,
    alpha: 0.4,
    dash: [2, 7],
    segs: [
      [0.4, 1.9],
      [2.6, 4.4],
      [4.9, 6]
    ]
  }
];
/** 门环上流动的光点:p 是在露出部分上的位置(0~1),v 是每秒走多少 */
const SPARKS = [
  { p: 0.05, v: 0.075 },
  { p: 0.42, v: 0.06 },
  { p: 0.73, v: 0.09 }
];

const CYAN = "92, 206, 245";
const PETAL_FLOAT = 4.5;

interface Ripple {
  x: number;
  y: number;
  t: number;
  life: number;
  size: number;
  power: number;
  rings: number;
}

interface Pulse {
  t: number;
  life: number;
  power: number;
  reach: number;
  dashed: boolean;
}

interface Petal {
  x: number;
  y: number;
  vx: number;
  vy: number;
  rot: number;
  spin: number;
  flip: number;
  sway: number;
  phase: number;
  size: number;
  sprite: number;
  landY: number;
  /** 落水后漂了多久,还在空中时为 -1 */
  floatT: number;
}

interface Mote {
  x: number;
  y: number;
  vx: number;
  vy: number;
  t: number;
  life: number;
  size: number;
  phase: number;
}

interface Tap {
  x: number;
  y: number;
  t: number;
  life: number;
}

const rand = (a: number, b: number) => a + Math.random() * (b - a);
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
const easeOut = (t: number) => 1 - (1 - t) ** 3;
const smooth = (t: number) => t * t * (3 - 2 * t);
/** 近似正态分布,范围约 [-1, 1] */
const gauss = () => (Math.random() + Math.random() + Math.random() - 1.5) / 1.5;

function inRect(
  x: number,
  y: number,
  r: { x0: number; x1: number; y0: number; y1: number }
) {
  return x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1;
}

/** 按 t / life 推进并移除到期的元素 */
function advance<T extends { t: number; life: number }>(list: T[], dt: number) {
  for (let i = list.length - 1; i >= 0; i--) {
    list[i].t += dt;
    if (list[i].t >= list[i].life) list.splice(i, 1);
  }
}

function makeGlow(dark: boolean): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(
    0,
    dark ? "rgba(235, 252, 255, 1)" : "rgba(120, 222, 252, 1)"
  );
  grad.addColorStop(
    0.2,
    dark ? "rgba(150, 230, 255, 0.85)" : "rgba(40, 175, 235, 0.9)"
  );
  grad.addColorStop(0.5, `rgba(${CYAN}, ${dark ? 0.3 : 0.4})`);
  grad.addColorStop(1, `rgba(${CYAN}, 0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  return c;
}

/** 梅花瓣:圆润的花瓣,顶端一个小缺口 */
function makePetal(inner: string, outer: string): HTMLCanvasElement {
  const s = 40;
  const c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  g.translate(s / 2, s / 2);
  const grad = g.createRadialGradient(0, s * 0.18, 0, 0, 0, s * 0.5);
  grad.addColorStop(0, inner);
  grad.addColorStop(1, outer);
  g.fillStyle = grad;
  g.beginPath();
  g.moveTo(0, s * 0.42);
  g.bezierCurveTo(s * 0.5, s * 0.22, s * 0.44, -s * 0.42, s * 0.08, -s * 0.36);
  g.lineTo(0, -s * 0.28);
  g.lineTo(-s * 0.08, -s * 0.36);
  g.bezierCurveTo(-s * 0.44, -s * 0.42, -s * 0.5, s * 0.22, 0, s * 0.42);
  g.fill();
  return c;
}

export function createSceneFx(
  canvas: HTMLCanvasElement,
  stage: HTMLElement,
  dark: boolean
): SceneFx | null {
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  let isDark = dark;
  let glow = makeGlow(isDark);
  const petalSprites = [
    makePetal("#fff1ee", "#e0645d"),
    makePetal("#fbd3cd", "#cc4b48"),
    makePetal("#fff8f6", "#ea8c86")
  ];
  // 从正下方开始顺时针的锥形渐变:门环埋在地下的部分透明,配合 globalAlpha 使用
  const hasConic = typeof ctx.createConicGradient === "function";
  let arcStroke: CanvasGradient | string = `rgb(${CYAN})`;
  if (hasConic) {
    const g = ctx.createConicGradient(Math.PI / 2, GATE.x, GATE.y);
    const c = (a: number) => `rgba(${CYAN}, ${a})`;
    g.addColorStop(0, c(0));
    g.addColorStop(0.11, c(0));
    g.addColorStop(0.22, c(1));
    g.addColorStop(0.78, c(1));
    g.addColorStop(0.89, c(0));
    g.addColorStop(1, c(0));
    arcStroke = g;
  }

  // 画布像素比、背景缩放比例、背景左上角(相对画布 / 相对视口)
  let dpr = 1;
  let k = 1;
  let ox = 0;
  let oy = 0;
  let stageLeft = 0;
  let stageTop = 0;
  let raf = 0;
  let last = 0;
  let clock = 0;
  let introT = 0;
  let pulseIn = 0.8;
  let pulseCount = 0;
  let rippleIn = 0.4;
  let petalIn = 0.5;
  let moteAcc = 0;
  let lastMove = { x: -1e4, y: -1e4, t: 0 };

  const ripples: Ripple[] = [];
  const pulses: Pulse[] = [];
  const petals: Petal[] = [];
  const motes: Mote[] = [];
  const taps: Tap[] = [];
  const sparks = SPARKS.map(s => ({ ...s }));

  function addRipple(
    x: number,
    y: number,
    power: number,
    size: number,
    rings = 3,
    life = rand(2.6, 3.6)
  ) {
    if (ripples.length > 28) ripples.shift();
    ripples.push({ x, y, t: 0, life, size, power, rings });
  }

  function randomRipple() {
    for (let i = 0; i < 6; i++) {
      const x = rand(WATER.x0, WATER.x1);
      const y = WATER.y0 + (WATER.y1 - WATER.y0) * Math.random() ** 0.8;
      if (WATER_SKIP.some(r => inRect(x, y, r))) continue;
      const depth = clamp01((y - WATER.y0) / (WATER.y1 - WATER.y0));
      addRipple(x, y, rand(0.6, 0.9), rand(52, 100) * (0.55 + 0.75 * depth));
      return;
    }
  }

  function addPetal() {
    petals.push({
      x: rand(PLUM.x0, PLUM.x1),
      y: rand(PLUM.y0, PLUM.y1),
      vx: rand(-26, -8),
      vy: rand(10, 20),
      rot: rand(0, Math.PI * 2),
      spin: rand(-1.4, 1.4),
      flip: rand(1.2, 2.6),
      sway: rand(8, 22),
      phase: rand(0, Math.PI * 2),
      size: rand(12, 20),
      sprite: Math.floor(Math.random() * petalSprites.length),
      landY: rand(WATER.y0 + 10, WATER.y1 - 30),
      floatT: -1
    });
  }

  function addMote() {
    let pick = Math.random() * MOTE_WEIGHT;
    const src = MOTE_SOURCES.find(s => (pick -= s.w) <= 0) ?? MOTE_SOURCES[0];
    motes.push({
      x: src.x + gauss() * src.sx,
      y: src.y + gauss() * src.sy,
      vx: rand(-4, 4),
      vy: rand(-26, -9),
      t: 0,
      life: rand(3, 7),
      size: rand(1.4, 3.2),
      phase: rand(0, Math.PI * 2)
    });
  }

  function update(dt: number) {
    clock += dt;
    introT += dt;

    // 圆波:每 3.4 秒一道,虚实交替
    if ((pulseIn -= dt) <= 0) {
      pulses.push({
        t: 0,
        life: 3.8,
        power: 0.65,
        reach: 100,
        dashed: pulseCount++ % 2 === 1
      });
      pulseIn = 3.4;
    }
    if ((rippleIn -= dt) <= 0) {
      randomRipple();
      rippleIn = rand(0.7, 1.7);
    }
    if ((petalIn -= dt) <= 0) {
      if (petals.length < 18) addPetal();
      petalIn = rand(0.6, 1.5);
    }
    moteAcc += dt * 7;
    while (moteAcc >= 1) {
      moteAcc -= 1;
      if (motes.length < 48) addMote();
    }

    for (const s of sparks) {
      s.p += s.v * dt;
      if (s.p > 1) s.p -= 1;
    }
    advance(pulses, dt);
    advance(ripples, dt);
    advance(taps, dt);
    advance(motes, dt);
    for (const m of motes) {
      m.x += (m.vx + Math.sin(clock * 1.3 + m.phase) * 5) * dt;
      m.y += m.vy * dt;
    }

    for (let i = petals.length - 1; i >= 0; i--) {
      const p = petals[i];
      if (p.floatT >= 0) {
        // 落水后随波漂一会儿再淡出
        p.floatT += dt;
        p.x -= 4 * dt;
        p.rot += p.spin * 0.08 * dt;
        if (p.floatT >= PETAL_FLOAT) petals.splice(i, 1);
        continue;
      }
      p.vy = Math.min(p.vy + 4 * dt, 34);
      p.x += (p.vx + Math.sin(clock * 1.1 + p.phase) * p.sway) * dt;
      p.y += p.vy * dt;
      p.rot += p.spin * dt;
      if (p.y >= p.landY && p.x >= WATER.x0 && p.x <= WATER.x1) {
        p.floatT = 0;
        addRipple(p.x, p.y, 0.55, 26 + p.size, 2, 2.2);
      } else if (p.y > 1120 || p.x < -40) {
        petals.splice(i, 1);
      }
    }
  }

  function ring(r: number) {
    ctx.beginPath();
    if (hasConic) ctx.arc(GATE.x, GATE.y, r, 0, Math.PI * 2);
    else ctx.arc(GATE.x, GATE.y, r, ARC_FROM, ARC_TO);
    ctx.stroke();
  }

  function drawRipples(px: number) {
    const hi = isDark ? "rgb(160, 225, 255)" : "rgb(255, 255, 255)";
    const lo = isDark ? "rgb(0, 0, 0)" : "rgb(62, 78, 90)";
    const tint = `rgb(${CYAN})`;
    ctx.lineWidth = Math.max(1.5, px);
    for (const r of ripples) {
      const f = r.t / r.life;
      const depth = clamp01((r.y - WATER.y0) / (WATER.y1 - WATER.y0));
      const flat = 0.2 + 0.17 * depth;
      const base = r.size * easeOut(f);
      const fade = r.power * (1 - f) ** 1.5 * smooth(clamp01(f / 0.08));
      for (let i = 0; i < r.rings; i++) {
        const rr = base - i * r.size * 0.22;
        if (rr <= 1) continue;
        const a = fade * (1 - i * 0.3);
        // 亮边 + 下方一道淡墨,像水墨里勾的波纹
        ctx.globalAlpha = a * (isDark ? 0.55 : 0.95);
        ctx.strokeStyle = hi;
        ctx.beginPath();
        ctx.ellipse(r.x, r.y, rr, rr * flat, 0, 0, Math.PI * 2);
        ctx.stroke();
        ctx.globalAlpha = a * (isDark ? 0.4 : 0.55);
        ctx.strokeStyle = lo;
        ctx.beginPath();
        ctx.ellipse(r.x, r.y + 1.6, rr, rr * flat, 0, 0, Math.PI * 2);
        ctx.stroke();
        if (i === 0) {
          ctx.globalAlpha = a * (isDark ? 0.45 : 0.4);
          ctx.strokeStyle = tint;
          ctx.beginPath();
          ctx.ellipse(
            r.x,
            r.y - 1,
            rr * 0.98,
            rr * flat * 0.98,
            0,
            0,
            Math.PI * 2
          );
          ctx.stroke();
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawPetals(floating: boolean) {
    for (const p of petals) {
      if (p.floatT >= 0 !== floating) continue;
      ctx.save();
      ctx.translate(p.x, p.y);
      if (floating) {
        // 浮在水面上:透视压扁,慢慢淡出
        ctx.globalAlpha = (1 - p.floatT / PETAL_FLOAT) * (isDark ? 0.7 : 0.9);
        ctx.scale(1, 0.45);
        ctx.rotate(p.rot);
      } else {
        ctx.globalAlpha = isDark ? 0.8 : 0.95;
        ctx.rotate(p.rot);
        // 翻转:横向缩放模拟花瓣在空中翻动
        ctx.scale(0.3 + 0.7 * Math.abs(Math.cos(clock * p.flip + p.phase)), 1);
      }
      ctx.drawImage(
        petalSprites[p.sprite],
        -p.size / 2,
        -p.size / 2,
        p.size,
        p.size
      );
      ctx.restore();
    }
  }

  function drawGate(px: number) {
    const intro = smooth(clamp01((introT - 0.2) / 1.6));
    if (intro <= 0) return;
    const boost = isDark ? 1.3 : 1;
    ctx.save();
    ctx.strokeStyle = arcStroke;
    ctx.lineCap = "round";

    // 门洞内沿的呼吸光
    const breath = 0.5 + 0.5 * Math.sin(clock * 1.2);
    ctx.globalAlpha = intro * (0.12 + 0.12 * breath) * boost;
    ctx.lineWidth = 9;
    ring(GATE.inner + 3);
    ctx.globalAlpha = intro * (0.35 + 0.25 * breath);
    ctx.lineWidth = Math.max(1.1, px);
    ring(GATE.inner - 2);

    // 刻度光弧:开场时从短到长展开
    for (const h of HUD) {
      const rot = clock * h.speed;
      ctx.globalAlpha = intro * h.alpha * boost;
      ctx.lineWidth = Math.max(h.width, px);
      ctx.setLineDash(h.dash);
      ctx.beginPath();
      for (const [a0, a1] of h.segs) {
        const from = a0 + rot;
        ctx.moveTo(
          GATE.x + h.r * Math.cos(from),
          GATE.y + h.r * Math.sin(from)
        );
        ctx.arc(GATE.x, GATE.y, h.r, from, from + (a1 - a0) * intro);
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // 内圈刻度线,每 6° 一根,每 30° 加长
    const tickR = HUD[0].r + 5;
    const rot = clock * HUD[0].speed;
    ctx.globalAlpha = intro * 0.3 * boost;
    ctx.lineWidth = Math.max(1, px);
    ctx.beginPath();
    for (let i = 0; i < 60; i++) {
      const a = rot + (i / 60) * Math.PI * 2;
      const len = i % 5 === 0 ? 7 : 3.5;
      const cos = Math.cos(a);
      const sin = Math.sin(a);
      ctx.moveTo(GATE.x + tickR * cos, GATE.y + tickR * sin);
      ctx.lineTo(GATE.x + (tickR + len) * cos, GATE.y + (tickR + len) * sin);
    }
    ctx.stroke();

    // 圆波
    for (const p of pulses) {
      if (p.t < 0) continue;
      const f = p.t / p.life;
      ctx.globalAlpha = p.power * (1 - f) ** 1.6 * boost;
      ctx.lineWidth = Math.max((1.8 - f) * 1.2, px);
      ctx.setLineDash(p.dashed ? [3, 8] : []);
      ring(GATE.outer + p.reach * easeOut(f));
    }
    ctx.setLineDash([]);
    ctx.restore();

    // 门环上流动的光点,带一段尾迹
    const mid = GATE.inner + (GATE.outer - GATE.inner) * 0.45;
    ctx.save();
    ctx.globalCompositeOperation = isDark ? "lighter" : "source-over";
    for (const s of sparks) {
      const fade = smooth(clamp01(Math.min(s.p, 1 - s.p) / 0.07)) * intro;
      for (let i = 7; i >= 0; i--) {
        const a = ARC_FROM + (s.p - i * 0.0045) * (ARC_TO - ARC_FROM);
        const size = i === 0 ? 22 : 14 - i;
        ctx.globalAlpha = fade * (i === 0 ? 0.95 : 0.5 * (1 - i / 8));
        ctx.drawImage(
          glow,
          GATE.x + mid * Math.cos(a) - size / 2,
          GATE.y + mid * Math.sin(a) - size / 2,
          size,
          size
        );
      }
    }
    ctx.restore();
  }

  function drawMotes() {
    ctx.save();
    ctx.globalCompositeOperation = isDark ? "lighter" : "source-over";
    for (const m of motes) {
      const f = m.t / m.life;
      const s = m.size * 8;
      ctx.globalAlpha =
        Math.sin(Math.PI * f) *
        (0.55 + 0.45 * Math.sin(clock * 5 + m.phase)) *
        (isDark ? 0.9 : 0.85);
      ctx.drawImage(glow, m.x - s / 2, m.y - s / 2, s, s);
    }
    ctx.restore();
  }

  function drawTaps(px: number) {
    ctx.save();
    ctx.strokeStyle = `rgb(${CYAN})`;
    ctx.lineWidth = Math.max(1.2, px);
    for (const p of taps) {
      const f = p.t / p.life;
      ctx.globalAlpha = 0.75 * (1 - f) ** 2;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 4 + 34 * easeOut(f), 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * ox, dpr * oy);
    // 1 个 CSS 像素在背景坐标里有多长:小屏上线条不至于细到看不见
    const px = 1 / k;
    drawRipples(px);
    drawPetals(true);
    drawGate(px);
    drawMotes();
    drawPetals(false);
    drawTaps(px);
  }

  function frame(now: number) {
    raf = requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    if (dt <= 0) return;
    update(dt);
    draw();
  }

  function start() {
    if (raf) return;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  function stop() {
    cancelAnimationFrame(raf);
    raf = 0;
  }

  function resize() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const s = stage.getBoundingClientRect();
    const c = canvas.getBoundingClientRect();
    k = s.width / REF_W;
    ox = s.left - c.left;
    oy = s.top - c.top;
    stageLeft = s.left;
    stageTop = s.top;
    if (!raf) draw();
  }

  const onVisibility = () => (document.hidden ? stop() : start());
  document.addEventListener("visibilitychange", onVisibility);

  // 先空跑两秒,让花瓣、萤光一开始就有,而不是从零慢慢长出来
  for (let i = 0; i < 60; i++) update(1 / 30);
  pulses.length = 0;
  pulseIn = 0.8;
  introT = 0;
  resize();
  start();

  return {
    resize,
    pointer(clientX, clientY, down) {
      const x = (clientX - stageLeft) / k;
      const y = (clientY - stageTop) / k;
      const onWater = x >= WATER.x0 && x <= WATER.x1 && y >= WATER.y0 - 10;
      if (down) {
        if (onWater) addRipple(x, y, 1, 110, 3, 3.2);
        else taps.push({ x, y, t: 0, life: 0.9 });
        return;
      }
      if (!onWater) return;
      if (clock - lastMove.t < 0.09) return;
      if (Math.hypot(x - lastMove.x, y - lastMove.y) < 28) return;
      lastMove = { x, y, t: clock };
      addRipple(x, y, 0.5, 52, 2, 2.4);
    },
    burst() {
      [0, 0.16, 0.32].forEach((delay, i) =>
        pulses.push({
          t: -delay,
          life: 2.4,
          power: 1,
          reach: 220,
          dashed: i === 1
        })
      );
      addRipple(GATE.x - 40, 812, 1, 150, 3, 3);
    },
    setDark(value) {
      isDark = value;
      glow = makeGlow(value);
    },
    destroy() {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    }
  };
}
