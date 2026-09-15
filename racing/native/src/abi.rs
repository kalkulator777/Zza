// Плоский ABI без wasm-bindgen.
//
// Правило номер один: модуль обязан иметь НОЛЬ импортов. Всё, что хозяин
// (Python через wasmtime или браузер) может сделать, — это позвать
// экспортированную функцию и читать/писать общую линейную память.
//
// Правило номер два: на 60 Гц нельзя делать десятки вызовов на кадр.
// Поэтому массивы ввода и вывода живут внутри модуля один раз, хозяин
// получает их адреса на старте и дальше работает с памятью напрямую:
// записал вводы -> один вызов rp_step -> прочитал состояния.

use core::mem::size_of;

/// Версия ABI. Хозяин обязан сверить, иначе молча разъедутся раскладки.
pub const ABI_VERSION: u32 = 1;

/// Максимум машин в мире. Восемь по контракту, держим запас.
pub const MAX_CARS: usize = 16;

/// Ввод одной машины. Ровно 16 байт, четыре f32.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CarInput {
    /// газ, 0..1
    pub throttle: f32,
    /// тормоз, 0..1
    pub brake: f32,
    /// руль, -1..1; плюс — налево, как в разделе 4 контракта
    pub steer: f32,
    /// ручник, 0..1
    pub handbrake: f32,
}

/// Телеметрия одного колеса. Восемь f32 = 32 байта.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct WheelOut {
    /// текущая длина подвески, м
    pub suspension_length: f32,
    /// ход подвески от полного отбоя, м (0 — вывешено, растёт при сжатии)
    pub suspension_travel: f32,
    /// сила подвески, Н
    pub suspension_force: f32,
    /// угол поворота колеса вокруг вертикали, рад
    pub steering: f32,
    /// угол проворота колеса вокруг оси, рад
    pub rotation: f32,
    /// продольный импульс на колесе, Н·с
    pub forward_impulse: f32,
    /// боковой импульс на колесе, Н·с
    pub side_impulse: f32,
    /// 1.0 — колесо на земле, 0.0 — в воздухе
    pub contact: f32,
}

/// Состояние одной машины наружу. 52 f32 = 208 байт.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CarOut {
    /// позиция центра масс, м
    pub px: f32,
    pub py: f32,
    pub pz: f32,
    /// курс вокруг Y, рад (для совместимости с разделом 4)
    pub yaw: f32,
    /// кватернион ориентации кузова
    pub qx: f32,
    pub qy: f32,
    pub qz: f32,
    pub qw: f32,
    /// линейная скорость, м/с
    pub vx: f32,
    pub vy: f32,
    pub vz: f32,
    /// модуль горизонтальной скорости, м/с
    pub speed: f32,
    /// угловая скорость, рад/с
    pub wx: f32,
    pub wy: f32,
    pub wz: f32,
    /// угол скольжения кузова, рад
    pub slip_angle: f32,
    /// условные обороты двигателя, об/мин
    pub engine_rpm: f32,
    /// сколько колёс на земле
    pub wheels_on_ground: f32,
    /// накопленный заряд заноса, с (аналог drift_charge из 6.3)
    pub drift_charge: f32,
    /// сторона заноса: +1 налево, -1 направо, 0 нет
    pub drift_dir: f32,
    /// четыре колеса: FL, FR, RL, RR
    pub wheels: [WheelOut; 4],
}

const _: () = assert!(size_of::<CarInput>() == 16);
const _: () = assert!(size_of::<CarOut>() == 208);

/// Массив вводов. Хозяин пишет сюда напрямую.
pub static mut INPUTS: [CarInput; MAX_CARS] = [CarInput {
    throttle: 0.0,
    brake: 0.0,
    steer: 0.0,
    handbrake: 0.0,
}; MAX_CARS];

/// Массив состояний. Хозяин читает отсюда напрямую.
pub static mut OUTPUTS: [CarOut; MAX_CARS] = [CarOut {
    px: 0.0,
    py: 0.0,
    pz: 0.0,
    yaw: 0.0,
    qx: 0.0,
    qy: 0.0,
    qz: 0.0,
    qw: 1.0,
    vx: 0.0,
    vy: 0.0,
    vz: 0.0,
    speed: 0.0,
    wx: 0.0,
    wy: 0.0,
    wz: 0.0,
    slip_angle: 0.0,
    engine_rpm: 0.0,
    wheels_on_ground: 0.0,
    drift_charge: 0.0,
    drift_dir: 0.0,
    wheels: [WheelOut {
        suspension_length: 0.0,
        suspension_travel: 0.0,
        suspension_force: 0.0,
        steering: 0.0,
        rotation: 0.0,
        forward_impulse: 0.0,
        side_impulse: 0.0,
        contact: 0.0,
    }; 4],
}; MAX_CARS];

/// Максимум подвижных предметов (конусы и прочее), чьи позы едут наружу.
pub const MAX_PROPS: usize = 64;

/// Поза одного подвижного предмета. 8 f32 = 32 байта.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct PropOut {
    pub px: f32,
    pub py: f32,
    pub pz: f32,
    /// полугабариты, чтобы рендер не хранил их отдельно
    pub hx: f32,
    pub qx: f32,
    pub qy: f32,
    pub qz: f32,
    pub qw: f32,
}

const _: () = assert!(size_of::<PropOut>() == 32);

/// Позы подвижных предметов. Хозяин читает напрямую.
pub static mut PROPS: [PropOut; MAX_PROPS] = [PropOut {
    px: 0.0,
    py: 0.0,
    pz: 0.0,
    hx: 0.0,
    qx: 0.0,
    qy: 0.0,
    qz: 0.0,
    qw: 1.0,
}; MAX_PROPS];

/// Неизменные размеры машины. Нужны рендеру, чтобы поставить колёса,
/// и хозяину, чтобы не дублировать константы из Rust. Читается один раз
/// после создания машины, в кадровом цикле не участвует.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CarDesc {
    pub half_width: f32,
    pub half_height: f32,
    pub half_length: f32,
    pub wheel_radius: f32,
    /// вынос колеса вбок от оси кузова, м
    pub axle_x: f32,
    /// вынос колеса вперёд от центра, м
    pub axle_z: f32,
    /// точка крепления подвески по Y в локальных координатах, м
    pub connection_y: f32,
    /// длина подвески в покое, м
    pub suspension_rest: f32,
}

const _: () = assert!(size_of::<CarDesc>() == 32);

/// Описания машин. Хозяин читает напрямую.
pub static mut DESCS: [CarDesc; MAX_CARS] = [CarDesc {
    half_width: 0.0,
    half_height: 0.0,
    half_length: 0.0,
    wheel_radius: 0.0,
    axle_x: 0.0,
    axle_z: 0.0,
    connection_y: 0.0,
    suspension_rest: 0.0,
}; MAX_CARS];

/// Полное состояние машины для отката. 24 f32 = 96 байт.
///
/// Важное наблюдение: контроллер колёс НЕ хранит физического состояния —
/// подвеска целиком пересчитывается лучом из позы кузова на каждом шаге.
/// Поэтому откат машины это «поза + скорости + четыре моих скаляра»,
/// а не сериализация всего мира.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CarSave {
    pub px: f32,
    pub py: f32,
    pub pz: f32,
    pub qx: f32,
    pub qy: f32,
    pub qz: f32,
    pub qw: f32,
    pub vx: f32,
    pub vy: f32,
    pub vz: f32,
    pub wx: f32,
    pub wy: f32,
    pub wz: f32,
    pub steer: f32,
    pub steer_angle: f32,
    pub drift_charge: f32,
    pub drift_dir: f32,
    /// углы проворота колёс — только для картинки
    pub wheel_rot: [f32; 4],
    pub _pad: [f32; 3],
}

const _: () = assert!(size_of::<CarSave>() == 96);

/// Слоты сохранений. Хозяин читает и пишет их напрямую.
pub static mut SAVES: [CarSave; MAX_CARS] = [CarSave {
    px: 0.0, py: 0.0, pz: 0.0,
    qx: 0.0, qy: 0.0, qz: 0.0, qw: 1.0,
    vx: 0.0, vy: 0.0, vz: 0.0,
    wx: 0.0, wy: 0.0, wz: 0.0,
    steer: 0.0, steer_angle: 0.0, drift_charge: 0.0, drift_dir: 0.0,
    wheel_rot: [0.0; 4],
    _pad: [0.0; 3],
}; MAX_CARS];

/// FNV-1a 64 по сырым байтам — единственный способ сверить состояние
/// бит в бит, не таща числа через текст.
pub fn fnv1a(seed: u64, bytes: &[u8]) -> u64 {
    let mut h = seed;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}
