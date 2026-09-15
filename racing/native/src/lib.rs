// racing_physics — обёртка вокруг Rapier с плоским ABI.
//
// Собирается под wasm32-unknown-unknown БЕЗ wasm-bindgen, чтобы один и тот же
// .wasm можно было запускать и в браузере, и в Python через wasmtime.
// Проверка «ноль импортов» — в tools проверочного стенда.

pub mod abi;
pub mod world;

use abi::{fnv1a, CarInput, CarOut, DESCS, INPUTS, MAX_CARS, OUTPUTS, PROPS, SAVES};
use world::{CarTuning, Preset, World, DT};

/// Единственный мир на модуль. Экземпляр wasm = одна симуляция,
/// так что таблица миров не нужна и ABI остаётся плоским.
static mut WORLD: Option<World> = None;

/// Буфер под заливку вершин полотна. Хозяин пишет сюда напрямую.
static mut VERT_STAGE: Vec<f32> = Vec::new();
static mut TRIS_STAGE: Vec<u32> = Vec::new();

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

/// Шаблон настроек, из которого берёт параметры следующая rp_car_spawn.
/// Плоский массив f32 в общей памяти: хозяин правит поля напрямую.
static mut TUNING: CarTuning = CarTuning::ARCADE;

/// Адрес шаблона настроек.
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
        VERT_STAGE = Vec::new();
        TRIS_STAGE = Vec::new();
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

/// Выделяет место под сетку полотна и отдаёт адрес буфера вершин
/// (nv троек f32). Хозяин заливает вершины ОДНИМ куском памяти.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_alloc_verts(nv: u32) -> u32 {
    unsafe {
        VERT_STAGE = vec![0.0f32; nv as usize * 3];
        VERT_STAGE.as_ptr() as u32
    }
}

/// То же для треугольников (nt троек u32).
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_alloc_tris(nt: u32) -> u32 {
    unsafe {
        TRIS_STAGE = vec![0u32; nt as usize * 3];
        TRIS_STAGE.as_ptr() as u32
    }
}

/// Строит треугольную сетку из залитых буферов. 0 — успех.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn rp_track_commit(friction: f32) -> u32 {
    let world = w();
    unsafe {
        let nv = VERT_STAGE.len() / 3;
        world.track_verts.clear();
        world.track_verts.reserve(nv);
        for i in 0..nv {
            world.track_verts.push(rapier3d::prelude::Vector::new(
                VERT_STAGE[i * 3],
                VERT_STAGE[i * 3 + 1],
                VERT_STAGE[i * 3 + 2],
            ));
        }
        let nt = TRIS_STAGE.len() / 3;
        world.track_tris.clear();
        world.track_tris.reserve(nt);
        for i in 0..nt {
            world
                .track_tris
                .push([TRIS_STAGE[i * 3], TRIS_STAGE[i * 3 + 1], TRIS_STAGE[i * 3 + 2]]);
        }
        VERT_STAGE = Vec::new();
        TRIS_STAGE = Vec::new();
    }
    world.commit_track(friction)
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
        for _ in 0..ticks {
            world.step(inp, out);
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
        fnv1a(0xcbf2_9ce4_8422_2325, bytes)
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
    let mut h = 0xcbf2_9ce4_8422_2325u64;
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
