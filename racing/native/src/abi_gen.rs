// ВЫПУЩЕНО tools/gen_abi.py из tools/abi_layout.json. Руками не править:
// правка переживёт ровно до первого `python3 tools/gen_abi.py --check`.
//
// Единственное описание раскладок общей памяти модуля физики.
// Из него tools/gen_abi.py выпускает native/src/abi_gen.rs, native/abi/abi_layout.py и native/abi/abi_layout.js.
// Руками правится ТОЛЬКО этот файл; всё остальное — вывод генератора, и `python3 tools/gen_abi.py --check` ловит расхождение.
// 
// ПРАВИЛО ВЕРСИИ (§12.24). abi_version поднимается при ЛЮБОМ изменении размера или раскладки любой структуры — даже если это чистое дополнение в хвост и по смыслу полей ничего не сломалось. Совместимость тут не по смыслу, а по байтам: версия отмечает не «формат несовместим», а «хозяин и модуль из разных сборок не должны молча заработать». Сценарий, ради которого поле и существует: браузер держит в кэше старый .wasm, а страница приезжает новая — новый хозяин читает CarOut как 54 f32 из модуля, который пишет 52, и последние два поля оказываются мусором. Искать это будут в физике.
// Версия 2 — этап 4e: CarInput 4->5 f32 (offtrack), CarOut 52->54 (boost_time, drift_level), CarTuning 32->40 (награда за занос).

use core::mem::{offset_of, size_of};

/// Версия раскладки. Хозяин обязан сверить, иначе молча разъедутся раскладки.
/// Поднимается при любом изменении размера ЛЮБОЙ структуры, даже дополнении в хвост:
/// совместимость тут по байтам, а не по смыслу полей (правило в шапке файла).
pub const ABI_VERSION: u32 = 2;

/// Максимум машин в мире. Восемь по контракту, держим запас.
pub const MAX_CARS: usize = 16;

/// Максимум подвижных предметов (конусы и прочее), чьи позы едут наружу.
pub const MAX_PROPS: usize = 64;

/// Сколько массивов по N f32 занимает залитая осевая линия: x, y, z, tx, tz, nx, nz, hw, s (12.1).
pub const TRACK_COLUMNS: usize = 9;

/// Столбцов вершин поперёк полотна: верх и низ левой стены, кромка, ось, кромка, низ и верх правой стены.
pub const TRACK_MESH_COLS: usize = 7;

/// Ввод одной машины.
/// 5 f32 = 20 байт.
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
    /// вне полотна: 1 — колёса на траве или у стены, 0 — на асфальте.
    /// Третий барьер 12.15: за занос ВНЕ трассы заряд не копится.
    /// Полотна модуль не знает (12.21), поэтому флаг приходит от хозяина
    /// по позе прошлого шага — одинаково у сервера и у клиента.
    /// Ноль по умолчанию: хозяин, который поля не знает, получает прежнее
    /// поведение, а не молча выключенный занос.
    pub offtrack: f32,
}

impl CarInput {
    /// Размер записи в байтах.
    pub const SIZE: usize = 20;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 5;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = CarInput {
        throttle: 0.0,
        brake: 0.0,
        steer: 0.0,
        handbrake: 0.0,
        offtrack: 0.0,
    };
}

const _: () = assert!(size_of::<CarInput>() == CarInput::SIZE);
const _: () = assert!(offset_of!(CarInput, throttle) == 0);
const _: () = assert!(offset_of!(CarInput, brake) == 4);
const _: () = assert!(offset_of!(CarInput, steer) == 8);
const _: () = assert!(offset_of!(CarInput, handbrake) == 12);
const _: () = assert!(offset_of!(CarInput, offtrack) == 16);

/// Телеметрия одного колеса.
/// 8 f32 = 32 байт.
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

impl WheelOut {
    /// Размер записи в байтах.
    pub const SIZE: usize = 32;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 8;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = WheelOut {
        suspension_length: 0.0,
        suspension_travel: 0.0,
        suspension_force: 0.0,
        steering: 0.0,
        rotation: 0.0,
        forward_impulse: 0.0,
        side_impulse: 0.0,
        contact: 0.0,
    };
}

const _: () = assert!(size_of::<WheelOut>() == WheelOut::SIZE);
const _: () = assert!(offset_of!(WheelOut, suspension_length) == 0);
const _: () = assert!(offset_of!(WheelOut, suspension_travel) == 4);
const _: () = assert!(offset_of!(WheelOut, suspension_force) == 8);
const _: () = assert!(offset_of!(WheelOut, steering) == 12);
const _: () = assert!(offset_of!(WheelOut, rotation) == 16);
const _: () = assert!(offset_of!(WheelOut, forward_impulse) == 20);
const _: () = assert!(offset_of!(WheelOut, side_impulse) == 24);
const _: () = assert!(offset_of!(WheelOut, contact) == 28);

/// Состояние одной машины наружу.
/// 54 f32 = 216 байт.
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
    /// остаток ускорения за занос, с (аналог boost_time из 6.1)
    pub boost_time: f32,
    /// уровень заноса, ВЫПЛАЧЕННЫЙ на этом шаге: 0, 1, 2 или 3.
    /// Держится один шаг — хозяин по нему шлёт race_event drift_boost.
    pub drift_level: f32,
    /// четыре колеса: FL, FR, RL, RR
    pub wheels: [WheelOut; 4],
}

impl CarOut {
    /// Размер записи в байтах.
    pub const SIZE: usize = 216;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 54;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = CarOut {
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
        boost_time: 0.0,
        drift_level: 0.0,
        wheels: [WheelOut::INIT; 4],
    };
}

const _: () = assert!(size_of::<CarOut>() == CarOut::SIZE);
const _: () = assert!(offset_of!(CarOut, px) == 0);
const _: () = assert!(offset_of!(CarOut, py) == 4);
const _: () = assert!(offset_of!(CarOut, pz) == 8);
const _: () = assert!(offset_of!(CarOut, yaw) == 12);
const _: () = assert!(offset_of!(CarOut, qx) == 16);
const _: () = assert!(offset_of!(CarOut, qy) == 20);
const _: () = assert!(offset_of!(CarOut, qz) == 24);
const _: () = assert!(offset_of!(CarOut, qw) == 28);
const _: () = assert!(offset_of!(CarOut, vx) == 32);
const _: () = assert!(offset_of!(CarOut, vy) == 36);
const _: () = assert!(offset_of!(CarOut, vz) == 40);
const _: () = assert!(offset_of!(CarOut, speed) == 44);
const _: () = assert!(offset_of!(CarOut, wx) == 48);
const _: () = assert!(offset_of!(CarOut, wy) == 52);
const _: () = assert!(offset_of!(CarOut, wz) == 56);
const _: () = assert!(offset_of!(CarOut, slip_angle) == 60);
const _: () = assert!(offset_of!(CarOut, engine_rpm) == 64);
const _: () = assert!(offset_of!(CarOut, wheels_on_ground) == 68);
const _: () = assert!(offset_of!(CarOut, drift_charge) == 72);
const _: () = assert!(offset_of!(CarOut, drift_dir) == 76);
const _: () = assert!(offset_of!(CarOut, boost_time) == 80);
const _: () = assert!(offset_of!(CarOut, drift_level) == 84);
const _: () = assert!(offset_of!(CarOut, wheels) == 88);

/// Неизменные размеры машины: рендеру — поставить колёса, хозяину — не дублировать константы из Rust.
/// 8 f32 = 32 байт.
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

impl CarDesc {
    /// Размер записи в байтах.
    pub const SIZE: usize = 32;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 8;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = CarDesc {
        half_width: 0.0,
        half_height: 0.0,
        half_length: 0.0,
        wheel_radius: 0.0,
        axle_x: 0.0,
        axle_z: 0.0,
        connection_y: 0.0,
        suspension_rest: 0.0,
    };
}

const _: () = assert!(size_of::<CarDesc>() == CarDesc::SIZE);
const _: () = assert!(offset_of!(CarDesc, half_width) == 0);
const _: () = assert!(offset_of!(CarDesc, half_height) == 4);
const _: () = assert!(offset_of!(CarDesc, half_length) == 8);
const _: () = assert!(offset_of!(CarDesc, wheel_radius) == 12);
const _: () = assert!(offset_of!(CarDesc, axle_x) == 16);
const _: () = assert!(offset_of!(CarDesc, axle_z) == 20);
const _: () = assert!(offset_of!(CarDesc, connection_y) == 24);
const _: () = assert!(offset_of!(CarDesc, suspension_rest) == 28);

/// Поза одного подвижного предмета.
/// 8 f32 = 32 байт.
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

impl PropOut {
    /// Размер записи в байтах.
    pub const SIZE: usize = 32;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 8;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = PropOut {
        px: 0.0,
        py: 0.0,
        pz: 0.0,
        hx: 0.0,
        qx: 0.0,
        qy: 0.0,
        qz: 0.0,
        qw: 1.0,
    };
}

const _: () = assert!(size_of::<PropOut>() == PropOut::SIZE);
const _: () = assert!(offset_of!(PropOut, px) == 0);
const _: () = assert!(offset_of!(PropOut, py) == 4);
const _: () = assert!(offset_of!(PropOut, pz) == 8);
const _: () = assert!(offset_of!(PropOut, hx) == 12);
const _: () = assert!(offset_of!(PropOut, qx) == 16);
const _: () = assert!(offset_of!(PropOut, qy) == 20);
const _: () = assert!(offset_of!(PropOut, qz) == 24);
const _: () = assert!(offset_of!(PropOut, qw) == 28);

/// Полное состояние машины для отката.
/// Контроллер колёс НЕ хранит физического состояния: подвеска целиком пересчитывается лучом из позы кузова на каждом шаге. Поэтому откат машины — это «поза + скорости + четыре моих скаляра», а не сериализация всего мира.
/// 24 f32 = 96 байт.
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
    /// остаток ускорения: без него откат отдавал бы заново
    /// уже потраченный буст
    pub boost_time: f32,
    pub _pad: [f32; 2],
}

impl CarSave {
    /// Размер записи в байтах.
    pub const SIZE: usize = 96;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 24;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = CarSave {
        px: 0.0,
        py: 0.0,
        pz: 0.0,
        qx: 0.0,
        qy: 0.0,
        qz: 0.0,
        qw: 1.0,
        vx: 0.0,
        vy: 0.0,
        vz: 0.0,
        wx: 0.0,
        wy: 0.0,
        wz: 0.0,
        steer: 0.0,
        steer_angle: 0.0,
        drift_charge: 0.0,
        drift_dir: 0.0,
        wheel_rot: [0.0; 4],
        boost_time: 0.0,
        _pad: [0.0; 2],
    };
}

const _: () = assert!(size_of::<CarSave>() == CarSave::SIZE);
const _: () = assert!(offset_of!(CarSave, px) == 0);
const _: () = assert!(offset_of!(CarSave, py) == 4);
const _: () = assert!(offset_of!(CarSave, pz) == 8);
const _: () = assert!(offset_of!(CarSave, qx) == 12);
const _: () = assert!(offset_of!(CarSave, qy) == 16);
const _: () = assert!(offset_of!(CarSave, qz) == 20);
const _: () = assert!(offset_of!(CarSave, qw) == 24);
const _: () = assert!(offset_of!(CarSave, vx) == 28);
const _: () = assert!(offset_of!(CarSave, vy) == 32);
const _: () = assert!(offset_of!(CarSave, vz) == 36);
const _: () = assert!(offset_of!(CarSave, wx) == 40);
const _: () = assert!(offset_of!(CarSave, wy) == 44);
const _: () = assert!(offset_of!(CarSave, wz) == 48);
const _: () = assert!(offset_of!(CarSave, steer) == 52);
const _: () = assert!(offset_of!(CarSave, steer_angle) == 56);
const _: () = assert!(offset_of!(CarSave, drift_charge) == 60);
const _: () = assert!(offset_of!(CarSave, drift_dir) == 64);
const _: () = assert!(offset_of!(CarSave, wheel_rot) == 68);
const _: () = assert!(offset_of!(CarSave, boost_time) == 84);
const _: () = assert!(offset_of!(CarSave, _pad) == 88);

/// Шаблон настроек машины. Хозяин правит поля напрямую в общей памяти, следующая rp_car_spawn берёт их отсюда.
/// 40 f32 = 160 байт.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CarTuning {
    /// --- кузов ---
    /// полугабариты кузова: длина/2 по Z, высота/2 по Y, ширина/2 по X
    pub half_length: f32,
    pub half_height: f32,
    pub half_width: f32,
    /// масса кузова, кг
    pub mass: f32,
    /// смещение центра масс вниз, м (устойчивость от переворота)
    pub com_drop: f32,
    /// --- подвеска ---
    pub suspension_rest: f32,
    pub suspension_stiffness: f32,
    pub suspension_compression: f32,
    pub suspension_damping: f32,
    pub max_suspension_travel: f32,
    pub max_suspension_force: f32,
    pub wheel_radius: f32,
    /// вынос колеса от центра: по Z (вперёд) и по X (влево)
    pub axle_z: f32,
    pub axle_x: f32,
    /// --- сцепление ---
    pub friction_slip: f32,
    pub side_friction_stiffness: f32,
    /// во сколько раз ручник роняет боковое сцепление задней оси
    pub handbrake_side_drop: f32,
    /// насколько ручник поднимает просимый доворот (аналог HANDBRAKE_TURN_GAIN)
    pub handbrake_turn_gain: f32,
    /// --- двигатель и тормоза ---
    /// максимальная тяга на ось, Н
    pub engine_force: f32,
    /// опорная скорость двигателя, м/с (тяга падает как 1 - v/max_speed)
    pub max_speed: f32,
    pub brake_force: f32,
    pub handbrake_force: f32,
    pub reverse_force: f32,
    /// --- руль ---
    /// максимальный угол поворота колёс, рад
    pub steer_max: f32,
    /// скорость подхода руля к цели, 1/с
    pub steer_rate: f32,
    pub steer_return: f32,
    /// во сколько раз ужимается руль на максимальной скорости
    pub steer_speed_falloff: f32,
    /// --- аркадная надстройка ---
    /// момент доворота по рулю, Н·м на рад невязки
    pub yaw_assist: f32,
    /// гашение паразитного рыскания, Н·м·с/рад
    pub yaw_damp: f32,
    /// прижим, Н на (м/с)^2
    pub downforce: f32,
    /// доля боковой скорости, снимаемая за шаг (аркадное «держит»)
    pub lateral_bite: f32,
    /// момент выравнивания кузова в воздухе, Н·м на рад
    pub air_righting: f32,
    /// --- награда за занос (6.3), числа приходят от хозяина ---
    /// Заряд в секундах на уровень 1, 2 и 3. Ноль — уровня нет:
    /// модуль игровых констант не знает (12.21), и пресет их не
    /// выдумывает. Хозяин кладёт сюда DRIFT_CHARGE_L* из
    /// game/physics.py и static/js/physics.js — те же числа 6.3.
    pub drift_charge_l1: f32,
    pub drift_charge_l2: f32,
    pub drift_charge_l3: f32,
    /// Сколько секунд ускорения даёт уровень 1, 2 и 3.
    pub drift_boost_l1: f32,
    pub drift_boost_l2: f32,
    pub drift_boost_l3: f32,
    /// к какой продольной скорости тянет ускорение, м/с.
    /// У каждой машины своя: stats.boost_speed из cars.json.
    pub boost_speed: f32,
    /// с каким ускорением тянет, м/с² (BOOST_ACCEL из 6.4).
    pub boost_accel: f32,
}

impl CarTuning {
    /// Размер записи в байтах.
    pub const SIZE: usize = 160;
    /// Сколько в записи чисел f32.
    pub const FLOATS: usize = 40;
    /// Запись «всё в нуле» — из неё набиваются общие буферы.
    pub const INIT: Self = CarTuning {
        half_length: 0.0,
        half_height: 0.0,
        half_width: 0.0,
        mass: 0.0,
        com_drop: 0.0,
        suspension_rest: 0.0,
        suspension_stiffness: 0.0,
        suspension_compression: 0.0,
        suspension_damping: 0.0,
        max_suspension_travel: 0.0,
        max_suspension_force: 0.0,
        wheel_radius: 0.0,
        axle_z: 0.0,
        axle_x: 0.0,
        friction_slip: 0.0,
        side_friction_stiffness: 0.0,
        handbrake_side_drop: 0.0,
        handbrake_turn_gain: 0.0,
        engine_force: 0.0,
        max_speed: 0.0,
        brake_force: 0.0,
        handbrake_force: 0.0,
        reverse_force: 0.0,
        steer_max: 0.0,
        steer_rate: 0.0,
        steer_return: 0.0,
        steer_speed_falloff: 0.0,
        yaw_assist: 0.0,
        yaw_damp: 0.0,
        downforce: 0.0,
        lateral_bite: 0.0,
        air_righting: 0.0,
        drift_charge_l1: 0.0,
        drift_charge_l2: 0.0,
        drift_charge_l3: 0.0,
        drift_boost_l1: 0.0,
        drift_boost_l2: 0.0,
        drift_boost_l3: 0.0,
        boost_speed: 0.0,
        boost_accel: 0.0,
    };
}

const _: () = assert!(size_of::<CarTuning>() == CarTuning::SIZE);
const _: () = assert!(offset_of!(CarTuning, half_length) == 0);
const _: () = assert!(offset_of!(CarTuning, half_height) == 4);
const _: () = assert!(offset_of!(CarTuning, half_width) == 8);
const _: () = assert!(offset_of!(CarTuning, mass) == 12);
const _: () = assert!(offset_of!(CarTuning, com_drop) == 16);
const _: () = assert!(offset_of!(CarTuning, suspension_rest) == 20);
const _: () = assert!(offset_of!(CarTuning, suspension_stiffness) == 24);
const _: () = assert!(offset_of!(CarTuning, suspension_compression) == 28);
const _: () = assert!(offset_of!(CarTuning, suspension_damping) == 32);
const _: () = assert!(offset_of!(CarTuning, max_suspension_travel) == 36);
const _: () = assert!(offset_of!(CarTuning, max_suspension_force) == 40);
const _: () = assert!(offset_of!(CarTuning, wheel_radius) == 44);
const _: () = assert!(offset_of!(CarTuning, axle_z) == 48);
const _: () = assert!(offset_of!(CarTuning, axle_x) == 52);
const _: () = assert!(offset_of!(CarTuning, friction_slip) == 56);
const _: () = assert!(offset_of!(CarTuning, side_friction_stiffness) == 60);
const _: () = assert!(offset_of!(CarTuning, handbrake_side_drop) == 64);
const _: () = assert!(offset_of!(CarTuning, handbrake_turn_gain) == 68);
const _: () = assert!(offset_of!(CarTuning, engine_force) == 72);
const _: () = assert!(offset_of!(CarTuning, max_speed) == 76);
const _: () = assert!(offset_of!(CarTuning, brake_force) == 80);
const _: () = assert!(offset_of!(CarTuning, handbrake_force) == 84);
const _: () = assert!(offset_of!(CarTuning, reverse_force) == 88);
const _: () = assert!(offset_of!(CarTuning, steer_max) == 92);
const _: () = assert!(offset_of!(CarTuning, steer_rate) == 96);
const _: () = assert!(offset_of!(CarTuning, steer_return) == 100);
const _: () = assert!(offset_of!(CarTuning, steer_speed_falloff) == 104);
const _: () = assert!(offset_of!(CarTuning, yaw_assist) == 108);
const _: () = assert!(offset_of!(CarTuning, yaw_damp) == 112);
const _: () = assert!(offset_of!(CarTuning, downforce) == 116);
const _: () = assert!(offset_of!(CarTuning, lateral_bite) == 120);
const _: () = assert!(offset_of!(CarTuning, air_righting) == 124);
const _: () = assert!(offset_of!(CarTuning, drift_charge_l1) == 128);
const _: () = assert!(offset_of!(CarTuning, drift_charge_l2) == 132);
const _: () = assert!(offset_of!(CarTuning, drift_charge_l3) == 136);
const _: () = assert!(offset_of!(CarTuning, drift_boost_l1) == 140);
const _: () = assert!(offset_of!(CarTuning, drift_boost_l2) == 144);
const _: () = assert!(offset_of!(CarTuning, drift_boost_l3) == 148);
const _: () = assert!(offset_of!(CarTuning, boost_speed) == 152);
const _: () = assert!(offset_of!(CarTuning, boost_accel) == 156);

// --- общие буферы -----------------------------------------------------------

/// Массив вводов. Хозяин пишет сюда напрямую. Адрес отдаёт rp_inputs_ptr().
pub static mut INPUTS: [CarInput; MAX_CARS] = [CarInput::INIT; MAX_CARS];

/// Массив состояний. Хозяин читает отсюда напрямую. Адрес отдаёт rp_outputs_ptr().
pub static mut OUTPUTS: [CarOut; MAX_CARS] = [CarOut::INIT; MAX_CARS];

/// Описания машин. Читается один раз после создания машины. Адрес отдаёт rp_descs_ptr().
pub static mut DESCS: [CarDesc; MAX_CARS] = [CarDesc::INIT; MAX_CARS];

/// Позы подвижных предметов. Хозяин читает напрямую. Адрес отдаёт rp_props_ptr().
pub static mut PROPS: [PropOut; MAX_PROPS] = [PropOut::INIT; MAX_PROPS];

/// Слоты сохранений. Хозяин читает и пишет их напрямую. Адрес отдаёт rp_saves_ptr().
pub static mut SAVES: [CarSave; MAX_CARS] = [CarSave::INIT; MAX_CARS];

/// Шаблон настроек, из которого берёт параметры следующая rp_car_spawn. Единственный буфер с ненулевым начальным значением: до первого rp_world_reset в нём лежит аркадный пресет. Адрес отдаёт rp_tuning_ptr().
pub static mut TUNING: CarTuning = CarTuning::ARCADE;
