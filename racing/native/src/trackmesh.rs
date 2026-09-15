// Постройка треугольной сетки полотна ВНУТРИ модуля.
//
// Зачем это здесь, а не у хозяина. Если сетку строит хозяин, её приходится
// строить дважды — в game/track.py и в static/js/track.js — и обе постройки
// обязаны совпасть до бита, иначе столкновения на сервере и на клиенте
// разъедутся. Это ровно та болезнь, от которой лечит этап: вместо двух
// физик, обязанных совпасть, получились бы две геометрии, обязанные совпасть.
//
// Поэтому хозяин заливает только осевую линию — те же девять массивов по N
// чисел, которые и так едут клиенту в race_init (12.1), — а вершины и
// треугольники считает модуль. Оба хозяина гоняют один и тот же .wasm,
// значит сетка у них одна и та же по построению, а не по договорённости.
//
// Про точность. Входные числа уже квантованы в f32 на стороне track.py
// (_f32 в 7.2) — ровно затем, чтобы Python и Float32Array держали одинаковые
// числа. Промежуточные выражения считаются в f64 и опускаются в f32 один раз,
// на записи вершины: это совпадает с тем, как ту же формулу посчитал бы любой
// хозяин (у Python и JS числа как раз f64), и не зависит от того, в каком
// порядке компилятор сложит f32. У wasm нет ни слитого умножения-сложения,
// ни расширенных регистров, так что результат один на всех движках.

use crate::abi::{fnv1a, FNV_SEED, TRACK_COLUMNS, TRACK_MESH_COLS};

/// Осевая линия как она залита хозяином: TRACK_COLUMNS массивов подряд,
/// в каждом по `n` чисел. Порядок массивов — из 12.1:
/// x, y, z, tx, tz, nx, nz, hw, s.
pub struct Centerline<'a> {
    pub n: usize,
    pub data: &'a [f32],
}

/// Номера массивов в залитом блоке.
pub const COL_X: usize = 0;
pub const COL_Y: usize = 1;
pub const COL_Z: usize = 2;
pub const COL_NX: usize = 5;
pub const COL_NZ: usize = 6;
pub const COL_HW: usize = 7;

impl<'a> Centerline<'a> {
    pub fn new(data: &'a [f32], n: usize) -> Option<Self> {
        if n < 3 || data.len() < n * TRACK_COLUMNS {
            return None;
        }
        Some(Centerline { n, data })
    }

    #[inline]
    fn at(&self, col: usize, i: usize) -> f64 {
        self.data[col * self.n + i] as f64
    }
}

/// Сколько вершин и треугольников даст осевая линия из `n` точек.
/// Семь столбцов поперёк, круг замкнут, поэтому колец ровно `n`.
pub const fn mesh_counts(n: usize) -> (usize, usize) {
    (n * TRACK_MESH_COLS, n * (TRACK_MESH_COLS - 1) * 2)
}

/// Разворачивает осевую линию в полотно с обочиной и вертикальными стенами.
///
/// Семь столбцов поперёк, слева направо в смысле раздела 4 (нормаль смотрит
/// влево, поэтому первый столбец — левый): верх левой стены, низ левой стены,
/// кромка асфальта, ось, кромка, низ правой стены, верх правой стены.
///
/// `wall_margin` — ширина обочины за кромкой (зона вылета из 12.7),
/// `wall_height` — высота стенки за обочиной. Обе величины хозяин берёт из
/// контракта и передаёт снаружи: модуль не должен знать игровых констант.
///
/// Вершины кладутся в `verts` тройками, треугольники в `tris` тройками
/// индексов. Оба буфера очищаются.
pub fn build(cl: &Centerline, wall_margin: f32, wall_height: f32,
             verts: &mut Vec<f32>, tris: &mut Vec<u32>) {
    let n = cl.n;
    let (nv, nt) = mesh_counts(n);
    verts.clear();
    verts.reserve(nv * 3);
    tris.clear();
    tris.reserve(nt * 3);

    let margin = wall_margin as f64;
    let height = wall_height as f64;

    for i in 0..n {
        let x = cl.at(COL_X, i);
        let y = cl.at(COL_Y, i);
        let z = cl.at(COL_Z, i);
        let nx = cl.at(COL_NX, i);
        let nz = cl.at(COL_NZ, i);
        let hw = cl.at(COL_HW, i);
        let outer = hw + margin;
        // (смещение поперёк, подъём над полотном) для каждого столбца
        let offs: [(f64, f64); TRACK_MESH_COLS] = [
            (outer, height),
            (outer, 0.0),
            (hw, 0.0),
            (0.0, 0.0),
            (-hw, 0.0),
            (-outer, 0.0),
            (-outer, height),
        ];
        for &(lateral, up) in offs.iter() {
            verts.push((x + nx * lateral) as f32);
            verts.push((y + up) as f32);
            verts.push((z + nz * lateral) as f32);
        }
    }

    let cols = TRACK_MESH_COLS as u32;
    for i in 0..n as u32 {
        // круг замкнут: за последним кольцом идёт нулевое
        let j = if i + 1 == n as u32 { 0 } else { i + 1 };
        for c in 0..cols - 1 {
            let a = i * cols + c;
            let b = i * cols + c + 1;
            let d = j * cols + c;
            let e = j * cols + c + 1;
            tris.push(a);
            tris.push(b);
            tris.push(e);
            tris.push(a);
            tris.push(e);
            tris.push(d);
        }
    }
}

/// FNV-1a по сырым байтам вершин (little-endian f32) — то же число, которое
/// считает по своей сетке хозяин. Единственный честный способ сказать
/// «сетка совпала», не таща 300 КБ чисел через текст.
pub fn verts_hash(verts: &[f32]) -> u64 {
    let mut h = FNV_SEED;
    for v in verts {
        h = fnv1a(h, &v.to_le_bytes());
    }
    h
}

/// То же по индексам треугольников (little-endian u32).
pub fn tris_hash(tris: &[u32]) -> u64 {
    let mut h = FNV_SEED;
    for t in tris {
        h = fnv1a(h, &t.to_le_bytes());
    }
    h
}
