// racing_physics — обёртка вокруг Rapier с плоским ABI.
//
// Собирается под wasm32-unknown-unknown БЕЗ wasm-bindgen, чтобы один и тот же
// .wasm можно было запускать и в браузере, и в Python через wasmtime.
// Проверка «ноль импортов» — в tools проверочного стенда.

pub mod abi;
#[rustfmt::skip]
pub mod abi_gen;
pub mod trackmesh;
pub mod world;

use abi::{fnv1a, CarEffect, CarInput, CarOut, FNV_SEED, DESCS, EFFECTS, INPUTS, MAX_CARS,
          OUTPUTS, PROPS, SAVES, TRACK_COLUMNS, TUNING};
use trackmesh::Centerline;
use world::{CarTuning, Preset, World, DT};

/// Единственный мир на модуль. Экземпляр wasm = одна симуляция,
/// так что таблица миров не нужна и ABI остаётся плоским.
static mut WORLD: Option<World> = None;

/// Буфер под заливку осевой линии: TRACK_COLUMNS массивов подряд по N f32.
/// Хозяин пишет сюда напрямую, одним куском памяти.
static mut CENTERLINE: Vec<f32> = Vec::new();
static mut CENTERLINE_N: u32 = 0;

/// Готовая сетка полотна. Остаётся в модуле после постройки: хозяину она
/// нужна не для физики (та уже внутри), а чтобы сверить сетку хэшем или
/// вычитать её при разборе полёта. Освобождается rp_track_free_mesh.
static mut MESH_VERTS: Vec<f32> = Vec::new();
static mut MESH_TRIS: Vec<u32> = Vec::new();

#[allow(static_mut_refs)]
fn w() -> &'static mut World {
    unsafe {
        if WORLD.is_none() {
            WORLD = Some(World::new(Preset::Arcade));
        }
        WORLD.as_mut().unwrap()
    }
}

// ---------------------------------------------------------------------------
// 1. Опознание и адреса общих буферов. Зовутся один раз на старте.
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn rp_abi_version() -> u32 {
    abi::ABI_VERSION
}

#[no_mangle]
pub extern "C" fn rp_max_cars() -> u32 {
    MAX_CARS as u32
}

/// Адрес массива вводов: MAX_CARS структур по 16 байт.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_inputs_ptr() -> u32 {
    unsafe { INPUTS.as_ptr() as u32 }
}

/// Адрес массива внешних воздействий: MAX_CARS структур по 48 байт.
///
/// Хозяин кладёт сюда дозу на один шаг ПЕРЕД `rp_step`, модуль применяет её
/// внутри шага и запись ОБНУЛЯЕТ. Правила — в описании `CarEffect`
/// (tools/abi_layout.json). `rp_step(n)` при n > 1 применит дозу только на
/// первом из n шагов: доза живёт один шаг, а не одну пачку.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_effects_ptr() -> u32 {
    unsafe { EFFECTS.as_ptr() as u32 }
}

/// Сколько в записи воздействия чисел f32 — чтобы хозяин не хардкодил.
#[no_mangle]
pub extern "C" fn rp_effect_floats() -> u32 {
    (core::mem::size_of::<CarEffect>() / 4) as u32
}

/// Адрес массива состояний: MAX_CARS структур по 192 байта.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_outputs_ptr() -> u32 {
    unsafe { OUTPUTS.as_ptr() as u32 }
}

/// Адрес массива слотов сохранения: MAX_CARS структур по 96 байт.
/// Через них делается откат для реконсиляции на клиенте.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_saves_ptr() -> u32 {
    unsafe { SAVES.as_ptr() as u32 }
}

/// Снять состояние машины в её слот.
#[no_mangle]
pub extern "C" fn rp_car_save(idx: u32) -> u32 {
    w().save_car(idx as usize)
}

/// Вернуть машину в состояние из её слота.
#[no_mangle]
pub extern "C" fn rp_car_restore(idx: u32) -> u32 {
    w().restore_car(idx as usize)
}

/// Адрес массива поз подвижных предметов: MAX_PROPS структур по 32 байта.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_props_ptr() -> u32 {
    unsafe { PROPS.as_ptr() as u32 }
}

#[no_mangle]
pub extern "C" fn rp_prop_count() -> u32 {
    w().props.len() as u32
}

/// Адрес шаблона настроек (сам буфер TUNING выпущен в abi_gen.rs).
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_tuning_ptr() -> u32 {
    unsafe { &TUNING as *const CarTuning as u32 }
}

/// Сколько в шаблоне полей типа f32.
#[no_mangle]
pub extern "C" fn rp_tuning_floats() -> u32 {
    (core::mem::size_of::<CarTuning>() / 4) as u32
}

/// Загрузить в шаблон готовый пресет: 0 — аркада, 1 — симулятор.
#[no_mangle]
pub extern "C" fn rp_tuning_preset(preset: u32) {
    unsafe {
        TUNING = if preset == 1 {
            CarTuning::SIM
        } else {
            CarTuning::ARCADE
        };
    }
}

/// Адрес массива описаний машин: MAX_CARS структур по 32 байта.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_descs_ptr() -> u32 {
    unsafe { DESCS.as_ptr() as u32 }
}

/// Размер одной записи вывода в байтах — чтобы хозяин не хардкодил.
#[no_mangle]
pub extern "C" fn rp_output_stride() -> u32 {
    core::mem::size_of::<CarOut>() as u32
}

// ---------------------------------------------------------------------------
// 2. Сборка мира.
// ---------------------------------------------------------------------------

/// Пересоздать мир. preset: 0 — аркада, 1 — симулятор.
#[no_mangle]
pub extern "C" fn rp_world_reset(preset: u32) {
    let p = if preset == 1 {
        Preset::Sim
    } else {
        Preset::Arcade
    };
    unsafe {
        WORLD = Some(World::new(p));
        TUNING = if preset == 1 {
            CarTuning::SIM
        } else {
            CarTuning::ARCADE
        };
        CENTERLINE = Vec::new();
        CENTERLINE_N = 0;
        MESH_VERTS = Vec::new();
        MESH_TRIS = Vec::new();
    }
}

#[no_mangle]
pub extern "C" fn rp_add_ground(half_size: f32, friction: f32) {
    w().add_ground_plane(half_size, friction);
}

/// Коробка. mass <= 0 — неподвижная, иначе твёрдое тело.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub extern "C" fn rp_add_box(
    hx: f32,
    hy: f32,
    hz: f32,
    x: f32,
    y: f32,
    z: f32,
    yaw: f32,
    pitch: f32,
    friction: f32,
    mass: f32,
) {
    w().add_box(hx, hy, hz, x, y, z, yaw, pitch, friction, mass);
}

/// Сколько массивов по N чисел ждёт rp_track_alloc_centerline.
#[no_mangle]
pub extern "C" fn rp_track_columns() -> u32 {
    TRACK_COLUMNS as u32
}

/// Обычный Rust-доступ к буферу осевой линии. Wasm-ABI ниже — тонкая
/// обёртка над этим; отдельная функция нужна нативным прогонам, где адрес
/// не влезает в u32.
#[allow(static_mut_refs)]
pub fn track_stage_centerline(n: u32) -> &'static mut [f32] {
    unsafe {
        CENTERLINE = vec![0.0f32; n as usize * TRACK_COLUMNS];
        CENTERLINE_N = n;
        &mut CENTERLINE
    }
}

/// Выделяет место под осевую линию и отдаёт адрес буфера: TRACK_COLUMNS
/// массивов подряд по `n` чисел каждый, в порядке 12.1 — x, y, z, tx, tz,
/// nx, nz, hw, s. Хозяин заливает те же квантованные числа, что уходят
/// клиенту в race_init, ОДНИМ куском памяти.
///
/// Осторожно: аллокация внутри модуля отсоединяет уже созданные на стороне
/// JS Float32Array. Виды надо пересоздавать после этого вызова.
#[no_mangle]
pub extern "C" fn rp_track_alloc_centerline(n: u32) -> u32 {
    track_stage_centerline(n).as_ptr() as u32
}

/// Строит полотно из залитой осевой линии и ставит его в мир.
///
/// `wall_margin` — обочина за кромкой (зона вылета из 12.7), `wall_height` —
/// высота стенки за обочиной, `friction` — трение полотна. Игровые константы
/// приходят снаружи: модуль их не знает и знать не должен.
///
/// 0 — успех, 1 — осевая линия не залита или короче трёх точек,
/// 2 — Rapier не принял сетку.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_build(wall_margin: f32, wall_height: f32, friction: f32) -> u32 {
    let world = w();
    unsafe {
        let n = CENTERLINE_N as usize;
        let cl = match Centerline::new(&CENTERLINE, n) {
            Some(c) => c,
            None => return 1,
        };
        trackmesh::build(&cl, wall_margin, wall_height, &mut MESH_VERTS, &mut MESH_TRIS);
        world.commit_track(&MESH_VERTS, &MESH_TRIS, friction)
    }
}

/// Сколько вершин в построенной сетке.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_vert_count() -> u32 {
    unsafe { (MESH_VERTS.len() / 3) as u32 }
}

/// Сколько треугольников в построенной сетке.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_tri_count() -> u32 {
    unsafe { (MESH_TRIS.len() / 3) as u32 }
}

/// Адрес вершин построенной сетки: троек f32 ровно rp_track_vert_count().
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_verts_ptr() -> u32 {
    unsafe { MESH_VERTS.as_ptr() as u32 }
}

/// Адрес треугольников: троек u32 ровно rp_track_tri_count().
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_tris_ptr() -> u32 {
    unsafe { MESH_TRIS.as_ptr() as u32 }
}

/// FNV-1a по байтам вершин. Это и есть ответ на вопрос «сетка та же самая?»
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_verts_hash() -> u64 {
    unsafe { trackmesh::verts_hash(&MESH_VERTS) }
}

#[no_mangle]
pub extern "C" fn rp_track_verts_hash_lo() -> u32 {
    rp_track_verts_hash() as u32
}

#[no_mangle]
pub extern "C" fn rp_track_verts_hash_hi() -> u32 {
    (rp_track_verts_hash() >> 32) as u32
}

/// То же по индексам треугольников.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_tris_hash() -> u64 {
    unsafe { trackmesh::tris_hash(&MESH_TRIS) }
}

#[no_mangle]
pub extern "C" fn rp_track_tris_hash_lo() -> u32 {
    rp_track_tris_hash() as u32
}

#[no_mangle]
pub extern "C" fn rp_track_tris_hash_hi() -> u32 {
    (rp_track_tris_hash() >> 32) as u32
}

/// Отдаёт память под осевой линией и копией сетки. Сама сетка уже внутри
/// Rapier, так что на физику это не влияет; §6 разведки напоминает, что
/// линейная память wasm умеет только расти, поэтому лишние сотни килобайт
/// лучше вернуть аллокатору до следующей трассы.
#[no_mangle]
pub extern "C" fn rp_track_free_mesh() {
    unsafe {
        CENTERLINE = Vec::new();
        CENTERLINE_N = 0;
        MESH_VERTS = Vec::new();
        MESH_TRIS = Vec::new();
    }
}

/// Ставит машину, возвращает её индекс.
#[no_mangle]
pub extern "C" fn rp_car_spawn(x: f32, y: f32, z: f32, yaw: f32) -> u32 {
    let t = unsafe { TUNING };
    w().spawn_car(x, y, z, yaw, t)
}

#[no_mangle]
pub extern "C" fn rp_car_count() -> u32 {
    w().cars.len() as u32
}

// ---------------------------------------------------------------------------
// 3. Горячий путь. Один вызов на кадр, всё остальное — через память.
// ---------------------------------------------------------------------------

/// Прогоняет `ticks` шагов по 1/60 с. Вводы берутся из INPUTS,
/// состояния пишутся в OUTPUTS. Возвращает номер тика.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_step(ticks: u32) -> u32 {
    let world = w();
    unsafe {
        let inp: &[CarInput] = &INPUTS;
        let out: &mut [CarOut] = &mut OUTPUTS;
        let eff: &mut [CarEffect] = &mut EFFECTS;
        for _ in 0..ticks {
            world.step(inp, out, eff);
        }
    }
    world.tick as u32
}

/// Пустой счётный цикл на f32 — эталон для сравнения движков между собой.
/// Нужен, чтобы отличить «Rapier медленный» от «этот движок медленный».
#[no_mangle]
pub extern "C" fn rp_bench_flops(n: u32) -> f32 {
    let mut a = 1.0f32;
    let mut b = 0.5f32;
    for i in 0..n {
        a = a * 0.999_999 + b * 0.000_001;
        b = b * 0.999_998 + (i as f32) * 1e-9;
        a = a.sqrt() + b;
        b = a * 0.5 - b * 0.25;
    }
    a + b
}

#[no_mangle]
pub extern "C" fn rp_dt() -> f32 {
    DT
}

// ---------------------------------------------------------------------------
// 4. Сверка бит в бит.
// ---------------------------------------------------------------------------

/// FNV-1a 64 по сырым байтам состояний всех машин.
/// Это и есть проверка «одинаково ли посчиталось в Python и в браузере».
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_state_hash() -> u64 {
    let n = w().cars.len();
    unsafe {
        let bytes = core::slice::from_raw_parts(
            OUTPUTS.as_ptr() as *const u8,
            n * core::mem::size_of::<CarOut>(),
        );
        fnv1a(FNV_SEED, bytes)
    }
}

/// Тот же хэш половинками — на случай движка без BigInt в i64.
#[no_mangle]
pub extern "C" fn rp_state_hash_lo() -> u32 {
    rp_state_hash() as u32
}

#[no_mangle]
pub extern "C" fn rp_state_hash_hi() -> u32 {
    (rp_state_hash() >> 32) as u32
}

/// Хэш ПОЛНОГО состояния тел, а не только выгрузки: позиции, повороты,
/// скорости всех твёрдых тел мира. Ловит расхождение раньше, чем оно
/// доедет до машин.
#[no_mangle]
pub extern "C" fn rp_world_hash() -> u64 {
    let world = w();
    let mut h = FNV_SEED;
    let mut buf = [0u8; 4];
    let mut push = |h: &mut u64, v: f32| {
        buf = v.to_le_bytes();
        *h = fnv1a(*h, &buf);
    };
    for (_, b) in world.bodies.iter() {
        let p = b.position();
        push(&mut h, p.translation.x);
        push(&mut h, p.translation.y);
        push(&mut h, p.translation.z);
        push(&mut h, p.rotation.x);
        push(&mut h, p.rotation.y);
        push(&mut h, p.rotation.z);
        push(&mut h, p.rotation.w);
        let v = b.linvel();
        push(&mut h, v.x);
        push(&mut h, v.y);
        push(&mut h, v.z);
        let a = b.angvel();
        push(&mut h, a.x);
        push(&mut h, a.y);
        push(&mut h, a.z);
    }
    h
}

#[no_mangle]
pub extern "C" fn rp_world_hash_lo() -> u32 {
    rp_world_hash() as u32
}

#[no_mangle]
pub extern "C" fn rp_world_hash_hi() -> u32 {
    (rp_world_hash() >> 32) as u32
}
