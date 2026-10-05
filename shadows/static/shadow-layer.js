"use strict";

/*
 * Слой теней для MapLibre (custom layer), считается на GPU.
 *
 * Алгоритм тот же, что в shadows/engine.py: здание — призма [min_height, height],
 * тень — основание, сдвинутое вдоль вектора от солнца на высоты min_height и height,
 * плюс полосы, заметаемые рёбрами. Сдвиг делает вершинный шейдер, поэтому при
 * смене времени геометрия не пересчитывается — меняется только uniform.
 *
 * Чтобы перекрывающиеся тени не темнели, геометрия рисуется в отдельный
 * framebuffer (маска 0/1) в проходе prerender, а в render маска один раз
 * накладывается на карту с нужной прозрачностью.
 */

const SHADOW_MAX_LENGTH_M = 800; // как MAX_SHADOW_LENGTH в engine.py
const SHADOW_MIN_ALTITUDE = 0.5; // как MIN_ALTITUDE в engine.py

const GEOM_VS = `
attribute vec2 a_pos;     // меркатор относительно u_origin
attribute float a_h;      // высота, на которую сдвигается вершина, м
attribute float a_scale;  // единиц меркатора в 1 м на широте здания
uniform mat4 u_matrix;
uniform vec2 u_vec;       // смещение тени (восток, -север) на 1 м высоты, м
uniform float u_max_h;    // ограничение длины тени
void main() {
  vec2 pos = a_pos + u_vec * min(a_h, u_max_h) * a_scale;
  gl_Position = u_matrix * vec4(pos, 0.0, 1.0);
}`;

const GEOM_FS = `
precision mediump float;
void main() { gl_FragColor = vec4(1.0); }`;

const COMPOSE_VS = `
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;

const COMPOSE_FS = `
precision mediump float;
uniform sampler2D u_mask;
uniform vec2 u_size;
uniform vec4 u_color; // premultiplied
void main() {
  float m = texture2D(u_mask, gl_FragCoord.xy / u_size).r;
  gl_FragColor = u_color * m;
}`;

class ShadowLayer {
  constructor({ id = "shadows", color = [0.12, 0.16, 0.27], opacity = 0.42 } = {}) {
    this.id = id;
    this.type = "custom";
    this.renderingMode = "2d";
    this.color = color;
    this.opacity = opacity;
    this.sun = null;
    this.vertexCount = 0;
    this.origin = [0, 0];
    this.pending = null;
  }

  onAdd(map, gl) {
    this.map = map;
    this.gl = gl;
    this.geomProgram = createProgram(gl, GEOM_VS, GEOM_FS);
    this.composeProgram = createProgram(gl, COMPOSE_VS, COMPOSE_FS);
    this.geomBuffer = gl.createBuffer();
    this.quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    this.fbo = null;
    if (this.pending) this.setBuildings(this.pending);
  }

  onRemove(_map, gl) {
    gl.deleteBuffer(this.geomBuffer);
    gl.deleteBuffer(this.quadBuffer);
    gl.deleteProgram(this.geomProgram);
    gl.deleteProgram(this.composeProgram);
    if (this.fbo) {
      gl.deleteFramebuffer(this.fbo.fb);
      gl.deleteTexture(this.fbo.tex);
    }
  }

  /** GeoJSON FeatureCollection зданий с properties.height / min_height. */
  setBuildings(fc) {
    if (!this.gl) {
      this.pending = fc;
      return;
    }
    this.pending = null;
    const data = buildGeometry(fc);
    this.origin = data.origin;
    this.vertexCount = data.vertices.length / 4;
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.geomBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data.vertices, gl.STATIC_DRAW);
    this.map.triggerRepaint();
  }

  /** Положение солнца в градусах; null — не рисовать. */
  setSun(azimuth, altitude) {
    this.sun = altitude > 0 ? { azimuth, altitude } : null;
    this.map?.triggerRepaint();
  }

  _ensureFbo(gl, w, h) {
    if (this.fbo && this.fbo.w === w && this.fbo.h === h) return;
    if (this.fbo) {
      gl.deleteFramebuffer(this.fbo.fb);
      gl.deleteTexture(this.fbo.tex);
    }
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    this.fbo = { fb, tex, w, h };
  }

  _visible() {
    return this.sun && this.vertexCount > 0;
  }

  prerender(gl, args) {
    if (!this._visible()) return;
    // Не трогаем VAO, привязанные MapLibre (WebGL2).
    if (gl.bindVertexArray) gl.bindVertexArray(null);
    const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
    const prevFb = gl.getParameter(gl.FRAMEBUFFER_BINDING);
    const prevViewport = gl.getParameter(gl.VIEWPORT);
    this._ensureFbo(gl, w, h);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo.fb);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.STENCIL_TEST);
    gl.disable(gl.BLEND);
    gl.disable(gl.CULL_FACE);
    gl.colorMask(true, true, true, true);

    const { azimuth, altitude } = this.sun;
    const alt = Math.max(altitude, SHADOW_MIN_ALTITUDE) * (Math.PI / 180);
    const az = azimuth * (Math.PI / 180);
    const len = 1 / Math.tan(alt);
    const east = -Math.sin(az) * len;
    const north = -Math.cos(az) * len;

    const p = this.geomProgram;
    gl.useProgram(p);
    gl.uniformMatrix4fv(p.u.u_matrix, false, translateMatrix(args.defaultProjectionData.mainMatrix, this.origin));
    gl.uniform2f(p.u.u_vec, east, -north); // ось y меркатора направлена на юг
    gl.uniform1f(p.u.u_max_h, SHADOW_MAX_LENGTH_M / len);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.geomBuffer);
    enableAttrib(gl, p.a.a_pos, 2, 16, 0);
    enableAttrib(gl, p.a.a_h, 1, 16, 8);
    enableAttrib(gl, p.a.a_scale, 1, 16, 12);
    gl.drawArrays(gl.TRIANGLES, 0, this.vertexCount);
    disableAttribs(gl, p);

    gl.bindFramebuffer(gl.FRAMEBUFFER, prevFb);
    gl.viewport(prevViewport[0], prevViewport[1], prevViewport[2], prevViewport[3]);
  }

  render(gl) {
    if (!this._visible() || !this.fbo) return;
    if (gl.bindVertexArray) gl.bindVertexArray(null);
    const p = this.composeProgram;
    gl.useProgram(p);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.STENCIL_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.fbo.tex);
    gl.uniform1i(p.u.u_mask, 0);
    gl.uniform2f(p.u.u_size, this.fbo.w, this.fbo.h);
    const [r, g, b] = this.color;
    const a = this.opacity;
    gl.uniform4f(p.u.u_color, r * a, g * a, b * a, a);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
    enableAttrib(gl, p.a.a_pos, 2, 8, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    disableAttribs(gl, p);
  }
}

// ------------------------------------------------------------------ geometry

function buildGeometry(fc) {
  const features = fc.features || [];
  // Начало координат — центр данных, чтобы float32 в шейдере хватало (точность ~мм).
  let sx = 0, sy = 0, n = 0;
  for (const f of features) {
    const c = firstCoord(f.geometry);
    if (!c) continue;
    const m = maplibregl.MercatorCoordinate.fromLngLat(c);
    sx += m.x; sy += m.y; n++;
  }
  const origin = n ? [sx / n, sy / n] : [0, 0];
  const out = new FloatArray();

  for (const f of features) {
    const g = f.geometry;
    if (!g) continue;
    const top = Number(f.properties.height) || 0;
    const low = Math.min(Number(f.properties.min_height) || 0, top);
    if (top <= 0) continue;
    const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
    for (const rings of polys) {
      const first = rings[0]?.[0];
      if (!first) continue;
      const scale = maplibregl.MercatorCoordinate.fromLngLat(first).meterInMercatorCoordinateUnits();
      const flat = [];
      const holes = [];
      const merc = rings.map((ring, ri) => {
        if (ri > 0) holes.push(flat.length / 2);
        const pts = [];
        // Последняя точка кольца повторяет первую — для earcut её не берём.
        for (let i = 0; i < ring.length - 1; i++) {
          const m = maplibregl.MercatorCoordinate.fromLngLat(ring[i]);
          const x = m.x - origin[0], y = m.y - origin[1];
          pts.push(x, y);
          flat.push(x, y);
        }
        return pts;
      });
      const tri = earcut(flat, holes);
      for (const h of [low, top]) {
        for (const i of tri) out.push(flat[2 * i], flat[2 * i + 1], h, scale);
      }
      for (const pts of merc) {
        const k = pts.length / 2;
        for (let i = 0; i < k; i++) {
          const j = (i + 1) % k;
          const ax = pts[2 * i], ay = pts[2 * i + 1], bx = pts[2 * j], by = pts[2 * j + 1];
          out.push(ax, ay, low, scale, bx, by, low, scale, bx, by, top, scale);
          out.push(ax, ay, low, scale, bx, by, top, scale, ax, ay, top, scale);
        }
      }
    }
  }
  return { origin, vertices: out.toArray() };
}

function firstCoord(g) {
  if (!g) return null;
  if (g.type === "Polygon") return g.coordinates[0]?.[0];
  if (g.type === "MultiPolygon") return g.coordinates[0]?.[0]?.[0];
  return null;
}

class FloatArray {
  constructor() {
    this.buf = new Float32Array(1 << 16);
    this.len = 0;
  }
  push(...vals) {
    if (this.len + vals.length > this.buf.length) {
      const next = new Float32Array(this.buf.length * 2);
      next.set(this.buf);
      this.buf = next;
    }
    for (const v of vals) this.buf[this.len++] = v;
  }
  toArray() {
    return this.buf.slice(0, this.len);
  }
}

// ---------------------------------------------------------------- gl helpers
// mainMatrix переводит меркатор 0..1 в clip space (modelViewProjectionMatrix в MapLibre 5 — в пикселях мира).

/** m × T(origin) в float64, результат — Float32Array для uniform. */
function translateMatrix(m, [ox, oy]) {
  const r = Float32Array.from(m);
  for (let i = 0; i < 4; i++) r[12 + i] = m[i] * ox + m[4 + i] * oy + m[12 + i];
  return r;
}

function createProgram(gl, vsSource, fsSource) {
  const compile = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl.VERTEX_SHADER, vsSource));
  gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fsSource));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  p.a = {};
  p.u = {};
  for (let i = 0; i < gl.getProgramParameter(p, gl.ACTIVE_ATTRIBUTES); i++) {
    const { name } = gl.getActiveAttrib(p, i);
    p.a[name] = gl.getAttribLocation(p, name);
  }
  for (let i = 0; i < gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS); i++) {
    const { name } = gl.getActiveUniform(p, i);
    p.u[name] = gl.getUniformLocation(p, name);
  }
  return p;
}

function enableAttrib(gl, loc, size, stride, offset) {
  if (loc === undefined || loc < 0) return;
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, offset);
}

function disableAttribs(gl, p) {
  for (const loc of Object.values(p.a)) if (loc >= 0) gl.disableVertexAttribArray(loc);
}
