(function installShaderBackground() {
  'use strict';

  // "Mesh drift" WebGL1 background (21st.dev Shader Builder recipe). Replaces
  // the earlier 2D star-field: same mounting point (.state-environment,
  // created by 10-state.js), same always-on lifecycle, but the moving field
  // itself is now this shader instead of drawn dots.
  const host = document.querySelector('.state-environment');
  if (!host || document.getElementById('shader-bg-canvas')) return;

  const canvas = document.createElement('canvas');
  canvas.id = 'shader-bg-canvas';
  canvas.className = 'shader-bg-canvas';
  host.prepend(canvas);

  const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
  if (!gl) { canvas.remove(); return; }

  const VERTEX_SRC = `
attribute vec2 a_position;
void main() { gl_Position = vec4(a_position, 0.0, 1.0); }
`;

  // Verbatim fragment shader supplied by the user (17.07 "Mesh drift" recipe).
  const FRAGMENT_SRC = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform vec3 u_colors[8];
uniform vec4 u_scene;      // resolution.xy, time, colour count
uniform vec4 u_shape;      // scale, intensity, paramA, warp
uniform vec4 u_surface;    // detail, contrast, brightness, saturation
uniform vec4 u_finish;     // hue, vignette, blur, grain
uniform vec4 u_transform;  // seed, rotation, drift, OKLab toggle
uniform vec4 u_space;      // offset.xy, pointer.xy
uniform vec4 u_cursor;

#define u_resolution u_scene.xy
#define u_time u_scene.z
#define u_colorCount u_scene.w
#define u_scale u_shape.x
#define u_intensity u_shape.y
#define u_paramA u_shape.z
#define u_warp u_shape.w
#define u_detail u_surface.x
#define u_contrast u_surface.y
#define u_brightness u_surface.z
#define u_saturation u_surface.w
#define u_hue u_finish.x
#define u_vignette u_finish.y
#define u_blur u_finish.z
#define u_grain u_finish.w
#ifdef GL_FRAGMENT_PRECISION_HIGH
#define u_seed u_transform.x
#else
#define u_seed mod(u_transform.x, 31.0)
#endif
#define u_rotate u_transform.y
#define u_drift u_transform.z
#define u_oklab u_transform.w
#define u_offset u_space.xy
#define u_mouse u_space.zw
#define u_cursorPresence u_cursor.x
#define u_cursorEffect u_cursor.y
#define u_cursorStrength u_cursor.z
#define u_cursorRadius u_cursor.w

float hash21(vec2 p) {
#ifndef GL_FRAGMENT_PRECISION_HIGH
  p = mod(p, 31.0);
#endif
  p = fract(p * vec2(234.34, 435.345));
  p += dot(p, p + 34.23);
  return fract(p.x * p.y);
}

float grainHash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

vec2 hash22(vec2 p) {
#ifndef GL_FRAGMENT_PRECISION_HIGH
  p = mod(p, 31.0);
#endif
  float n = sin(dot(p, vec2(41.0, 289.0)));
  return fract(vec2(15731.743, 7892.321) * n);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash21(i), hash21(i + vec2(1.0, 0.0)), u.x),
    mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), u.x),
    u.y);
}

float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.5;
  for (int i = 0; i < 5; i++) {
    v += a * noise(p);
    p = p * 2.03 + vec2(17.0, 9.2);
    a *= 0.5;
  }
  return v;
}

vec3 srgbToLinear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)),
    step(0.04045, c));
}
vec3 linearToSrgb(vec3 c) {
  return mix(c * 12.92, 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055,
    step(0.0031308, c));
}
vec3 linToOklab(vec3 c) {
  float l = 0.4122214708 * c.r + 0.5363325363 * c.g + 0.0514459929 * c.b;
  float m = 0.2119034982 * c.r + 0.6806995451 * c.g + 0.1073969566 * c.b;
  float s = 0.0883024619 * c.r + 0.2817188376 * c.g + 0.6299787005 * c.b;
  l = pow(max(l, 0.0), 1.0 / 3.0);
  m = pow(max(m, 0.0), 1.0 / 3.0);
  s = pow(max(s, 0.0), 1.0 / 3.0);
  return vec3(
    0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s);
}
vec3 oklabToLin(vec3 c) {
  float l = c.x + 0.3963377774 * c.y + 0.2158037573 * c.z;
  float m = c.x - 0.1055613458 * c.y - 0.0638541728 * c.z;
  float s = c.x - 0.0894841775 * c.y - 1.2914855480 * c.z;
  l = l * l * l; m = m * m * m; s = s * s * s;
  return vec3(
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s);
}
vec3 mixColour(vec3 a, vec3 b, float t) {
  if (u_oklab > 0.5) {
    vec3 la = linToOklab(srgbToLinear(a));
    vec3 lb = linToOklab(srgbToLinear(b));
    return clamp(linearToSrgb(oklabToLin(mix(la, lb, t))), 0.0, 1.0);
  }
  return mix(a, b, t);
}

vec3 palette(float x) {
  float n = max(u_colorCount - 1.0, 1.0);
  float f = clamp(x, 0.0, 1.0) * n;
  vec3 col = u_colors[0];
  for (int i = 0; i < 7; i++) {
    if (float(i) < n)
      col = mixColour(col, u_colors[i + 1],
        smoothstep(0.0, 1.0, clamp(f - float(i), 0.0, 1.0)));
  }
  return col;
}

vec3 hueRotate(vec3 col, float a) {
  const mat3 toYIQ = mat3(0.299, 0.596, 0.211,
                          0.587, -0.274, -0.523,
                          0.114, -0.322, 0.312);
  const mat3 toRGB = mat3(1.0, 1.0, 1.0,
                          0.956, -0.272, -1.106,
                          0.621, -0.647, 1.703);
  vec3 yiq = toYIQ * col;
  float ca = cos(a), sa = sin(a);
  yiq = vec3(yiq.x, yiq.y * ca - yiq.z * sa, yiq.y * sa + yiq.z * ca);
  return toRGB * yiq;
}

vec3 shade(vec2 uv, vec2 p, float t) {
  vec3 acc = u_colors[0] * 0.15;
  float total = 0.15;
  for (int i = 0; i < 8; i++) {
    if (float(i) >= u_colorCount) break;
    float fi = float(i);
    vec2 c = vec2(
      sin(t * (0.21 + fi * 0.071) + fi * 2.4 + u_seed),
      cos(t * (0.17 + fi * 0.093) + fi * 1.7)) * (0.45 + u_intensity * 0.35);
    float w = exp(-dot(p - c, p - c) * 6.0);
    acc += u_colors[i] * w;
    total += w;
  }
  return acc / total;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution.xy;
  vec2 screenUv = uv;
  vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution.xy)
    / min(u_resolution.x, u_resolution.y);
  float cursorMask = 0.0;

  if (u_cursorPresence > 0.001) {
    vec2 cursor = (0.5 * u_mouse * u_resolution.xy)
      / min(u_resolution.x, u_resolution.y);
    vec2 cursorDelta = p - cursor;
    if (u_cursorEffect < 0.5) {
      p += cursor * u_cursorPresence * u_cursorStrength * 0.55;
    } else {
      float cursorDistance = length(cursorDelta);
      vec2 cursorDirection = cursorDelta / max(cursorDistance, 0.0001);
      cursorMask = u_cursorPresence
        * (1.0 - smoothstep(0.0, u_cursorRadius, cursorDistance));
      if (u_cursorEffect < 1.5) {
        p -= cursorDirection * cursorMask * u_cursorStrength * 0.24;
      } else if (u_cursorEffect < 2.5) {
        float cursorAngle = cursorMask * u_cursorStrength * 2.2;
        float cc = cos(cursorAngle), cs = sin(cursorAngle);
        p = cursor + mat2(cc, -cs, cs, cc) * cursorDelta;
      } else if (u_cursorEffect < 3.5) {
        float ripple = sin(
          cursorDistance / max(u_cursorRadius, 0.001) * 18.0 - u_time * 5.0);
        p -= cursorDirection * ripple * cursorMask * u_cursorStrength * 0.07;
      }
    }
  }

  uv = p * min(u_resolution.x, u_resolution.y) / u_resolution.xy + 0.5;
  p *= u_scale;
  if (abs(u_rotate) > 0.0001) {
    float cr = cos(u_rotate), sr = sin(u_rotate);
    p = mat2(cr, -sr, sr, cr) * p;
  }
  p += u_offset;
  if (u_drift > 0.0001)
    p += u_drift * vec2(sin(u_time * 0.31), cos(u_time * 0.23));
  if (u_warp > 0.0) {
    p += u_warp * (vec2(
      fbm(p * u_detail + u_seed),
      fbm(p * u_detail + vec2(5.2, 1.3))) - 0.5);
  }
  vec3 col;
  if (u_blur > 0.0) {
    float e = u_blur;
    float pe = e * u_scale;
    vec2 uvE = vec2(e) * min(u_resolution.x, u_resolution.y) / u_resolution.xy;
    col  = shade(uv, p, u_time) * 0.36;
    col += shade(uv + vec2(uvE.x, 0.0), p + vec2(pe, 0.0), u_time) * 0.16;
    col += shade(uv - vec2(uvE.x, 0.0), p - vec2(pe, 0.0), u_time) * 0.16;
    col += shade(uv + vec2(0.0, uvE.y), p + vec2(0.0, pe), u_time) * 0.16;
    col += shade(uv - vec2(0.0, uvE.y), p - vec2(0.0, pe), u_time) * 0.16;
  } else {
    col = shade(uv, p, u_time);
  }
  if (abs(u_contrast - 1.0) > 0.0001)
    col = (col - 0.5) * u_contrast + 0.5;
  if (abs(u_saturation - 1.0) > 0.0001) {
    float luma = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(vec3(luma), col, u_saturation);
  }
  if (abs(u_hue) > 0.0001)
    col = hueRotate(col, u_hue);
  if (abs(u_brightness) > 0.0001)
    col += u_brightness;
  if (u_vignette > 0.0001) {
    float vd = length(screenUv - 0.5) * 1.41421356;
    col *= 1.0 - u_vignette * smoothstep(0.35, 1.0, vd);
  }
  if (u_cursorPresence > 0.001 && u_cursorEffect > 3.5)
    col += (vec3(0.18) + col * 0.12) * cursorMask * u_cursorStrength;
  if (u_grain > 0.0001)
    col += (grainHash(
      gl_FragCoord.xy + vec2(u_seed * 17.0, u_seed * 31.0)) - 0.5) * u_grain;
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
`;

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.warn('shader-bg: compile failed', gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  const vertexShader = compile(gl.VERTEX_SHADER, VERTEX_SRC);
  const fragmentShader = compile(gl.FRAGMENT_SHADER, FRAGMENT_SRC);
  if (!vertexShader || !fragmentShader) { canvas.remove(); return; }

  const program = gl.createProgram();
  gl.attachShader(program, vertexShader);
  gl.attachShader(program, fragmentShader);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn('shader-bg: link failed', gl.getProgramInfoLog(program));
    canvas.remove();
    return;
  }
  gl.useProgram(program);

  // Fullscreen triangle (WebGL1 has no gl_VertexID — a single oversized
  // triangle avoids the diagonal seam a two-triangle quad would need).
  const positionBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const positionLoc = gl.getAttribLocation(program, 'a_position');
  gl.enableVertexAttribArray(positionLoc);
  gl.vertexAttribPointer(positionLoc, 2, gl.FLOAT, false, 0, 0);

  const uColors = gl.getUniformLocation(program, 'u_colors[0]');
  const uScene = gl.getUniformLocation(program, 'u_scene');
  const uShape = gl.getUniformLocation(program, 'u_shape');
  const uSurface = gl.getUniformLocation(program, 'u_surface');
  const uFinish = gl.getUniformLocation(program, 'u_finish');
  const uTransform = gl.getUniformLocation(program, 'u_transform');
  const uSpace = gl.getUniformLocation(program, 'u_space');
  const uCursor = gl.getUniformLocation(program, 'u_cursor');

  // #10002B, #7F00FF, #33AEB9, #0920F4 — first 4 slots; rest unused (colorCount=4).
  const colors = new Float32Array([
    0.063, 0.000, 0.169,
    0.498, 0.000, 1.000,
    0.200, 0.682, 0.725,
    0.035, 0.125, 0.957,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
  ]);
  gl.uniform3fv(uColors, colors);

  const SHAPE = [1.10, 0.34, 0.50, 0.00];
  const SURFACE = [2.40, 0.96, -0.10, 0.96];
  const FINISH = [0.00, 0.36, 0.026, 0.07];
  const TRANSFORM = [1453.0, 0.00, 0.00, 0.0];
  const CURSOR = [0.0, 2.0, 0.65, 0.46]; // presence 0 — cursor off, per spec

  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const DPR_CAP = 2;
  let width = 0, height = 0, frame = 0, startTime = 0;

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, DPR_CAP);
    width = Math.round(innerWidth * dpr);
    height = Math.round(innerHeight * dpr);
    canvas.width = width; canvas.height = height;
    canvas.style.width = `${innerWidth}px`;
    canvas.style.height = `${innerHeight}px`;
    gl.viewport(0, 0, width, height);
  }

  function draw(seconds) {
    gl.uniform4f(uScene, width, height, seconds * 0.73, 4.0);
    gl.uniform4f(uShape, SHAPE[0], SHAPE[1], SHAPE[2], SHAPE[3]);
    gl.uniform4f(uSurface, SURFACE[0], SURFACE[1], SURFACE[2], SURFACE[3]);
    gl.uniform4f(uFinish, FINISH[0], FINISH[1], FINISH[2], FINISH[3]);
    gl.uniform4f(uTransform, TRANSFORM[0], TRANSFORM[1], TRANSFORM[2], TRANSFORM[3]);
    gl.uniform4f(uSpace, 0, 0, 0, 0);
    gl.uniform4f(uCursor, CURSOR[0], CURSOR[1], CURSOR[2], CURSOR[3]);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function animate(now) {
    frame = requestAnimationFrame(animate);
    if (!startTime) startTime = now;
    draw((now - startTime) / 1000);
  }

  function sync() {
    cancelAnimationFrame(frame);
    if (document.hidden) return;
    if (reduce.matches) { draw(0); return; } // static frame, no loop
    frame = requestAnimationFrame(animate);
  }

  let resizeTimer = 0;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); draw((performance.now() - (startTime || performance.now())) / 1000); }, 120);
  }, { passive: true });
  document.addEventListener('visibilitychange', sync);
  reduce.addEventListener?.('change', sync);

  resize();
  sync();

  // ---------- Звёздный слой поверх шейдера ----------
  // «Космос со звёздами»: шейдер даёт едва заметное свечение млечного пути,
  // а звёзды возвращают глубину. Код звёзд — восстановленный approved-слой
  // 72e999b (мерцание + медленный дрейф, DPR 1.25, ~19fps троттлинг).
  (function installStars() {
    const starCanvas = document.createElement('canvas');
    starCanvas.className = 'star-layer';
    host.append(starCanvas);
    const context = starCanvas.getContext('2d', { alpha: true });
    const DENSITY = 0.00018, TWINKLE_P = 0.55, DRIFT = 3.5, TINT = '216, 220, 227';
    let sw = 0, sh = 0, stars = [], starFrame = 0, lastTime = 0, lastFrame = 0;
    const build = () => {
      const count = Math.floor(sw * sh * DENSITY);
      stars = Array.from({ length: count }, () => {
        const angle = Math.random() * Math.PI * 2, speed = DRIFT * (0.4 + Math.random() * 0.8), tw = Math.random() < TWINKLE_P;
        return { x: Math.random() * sw, y: Math.random() * sh, r: Math.random() * 0.6 + 0.55, base: Math.random() * 0.36 + 0.24, tw: tw ? 0.5 + Math.random() * 0.5 : null, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed };
      });
    };
    const render = dt => {
      context.clearRect(0, 0, sw, sh);
      for (const st of stars) {
        if (dt) {
          st.x += st.vx * dt; st.y += st.vy * dt;
          if (st.x < 0) st.x += sw; else if (st.x > sw) st.x -= sw;
          if (st.y < 0) st.y += sh; else if (st.y > sh) st.y -= sh;
        }
        let o = st.base; if (st.tw !== null) o = 0.22 + Math.abs(Math.sin((Date.now() * 0.001) / st.tw) * 0.38);
        context.beginPath(); context.fillStyle = `rgba(${TINT}, ${o})`; context.arc(st.x, st.y, st.r, 0, Math.PI * 2); context.fill();
      }
    };
    const starResize = () => {
      const dpr = Math.min(devicePixelRatio || 1, 1.25); sw = innerWidth; sh = innerHeight;
      starCanvas.width = Math.round(sw * dpr); starCanvas.height = Math.round(sh * dpr);
      starCanvas.style.width = `${sw}px`; starCanvas.style.height = `${sh}px`;
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      build(); render(0);
    };
    const starAnimate = time => {
      starFrame = requestAnimationFrame(starAnimate);
      if (lastFrame && time - lastFrame < 52) return;
      const dt = lastTime ? Math.min((time - lastTime) / 1000, 0.1) : 0; lastTime = time; lastFrame = time; render(dt);
    };
    const starSync = () => {
      cancelAnimationFrame(starFrame); lastTime = 0; lastFrame = 0;
      if (document.hidden) return;
      if (reduce.matches) { render(0); return; }
      starFrame = requestAnimationFrame(starAnimate);
    };
    let starResizeTimer = 0;
    addEventListener('resize', () => { clearTimeout(starResizeTimer); starResizeTimer = setTimeout(starResize, 120); }, { passive: true });
    document.addEventListener('visibilitychange', starSync);
    reduce.addEventListener?.('change', starSync);
    starResize();
    starSync();
  })();
})();
