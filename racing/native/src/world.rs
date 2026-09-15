// Мир Rapier: полотно трассы треугольной сеткой, препятствия кубами,
// машины на встроенном DynamicRayCastVehicleController плюс аркадная
// надстройка поверх твёрдого тела.

use rapier3d::control::{DynamicRayCastVehicleController, WheelTuning};
use rapier3d::prelude::*;

use crate::abi::{CarDesc, CarInput, CarOut, CarSave, PropOut, DESCS, MAX_CARS, MAX_PROPS, PROPS, SAVES};

/// Шаг симуляции жёстко фиксирован: 60 Гц, как в контракте.
pub const DT: f32 = 1.0 / 60.0;

/// Пресет управляемости.
#[derive(Clone, Copy, PartialEq)]
pub enum Preset {
    /// «Аркада»: жёстко, цепко, помощь в руле.
    Arcade,
    /// «Симулятор»: честно, без помощи.
    Sim,
}

/// Настройки машины. Всё, что различает пресеты и модели машин,
/// собрано здесь — чтобы не искать магические числа по коду.
///
/// Раскладка `repr(C)` из одних f32 — это ещё и часть ABI: хозяин видит
/// эту структуру как плоский массив чисел в общей памяти и правит её
/// напрямую, без единого дополнительного вызова. Так «аркада» и
/// «симулятор» становятся данными, а не кодом.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct CarTuning {
    // --- кузов ---
    /// полугабариты кузова: длина/2 по Z, высота/2 по Y, ширина/2 по X
    pub half_length: f32,
    pub half_height: f32,
    pub half_width: f32,
    /// масса кузова, кг
    pub mass: f32,
    /// смещение центра масс вниз, м (устойчивость от переворота)
    pub com_drop: f32,

    // --- подвеска ---
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

    // --- сцепление ---
    pub friction_slip: f32,
    pub side_friction_stiffness: f32,
    /// во сколько раз ручник роняет боковое сцепление задней оси
    pub handbrake_side_drop: f32,
    /// насколько ручник поднимает просимый доворот (аналог HANDBRAKE_TURN_GAIN)
    pub handbrake_turn_gain: f32,

    // --- двигатель и тормоза ---
    /// максимальная тяга на ось, Н
    pub engine_force: f32,
    /// опорная скорость двигателя, м/с (тяга падает как 1 - v/max_speed)
    pub max_speed: f32,
    pub brake_force: f32,
    pub handbrake_force: f32,
    pub reverse_force: f32,

    // --- руль ---
    /// максимальный угол поворота колёс, рад
    pub steer_max: f32,
    /// скорость подхода руля к цели, 1/с
    pub steer_rate: f32,
    pub steer_return: f32,
    /// во сколько раз ужимается руль на максимальной скорости
    pub steer_speed_falloff: f32,

    // --- аркадная надстройка ---
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
}

impl CarTuning {
    /// «Симулятор»: мягче подвеска, живое сцепление, никакой помощи в руле.
    pub const SIM: CarTuning = CarTuning {
        half_length: 2.0,
        half_height: 0.42,
        half_width: 0.78,
        mass: 1200.0,
        com_drop: 0.28,

        suspension_rest: 0.34,
        suspension_stiffness: 30.0,
        suspension_compression: 2.2,
        suspension_damping: 2.8,
        max_suspension_travel: 0.20,
        max_suspension_force: 30000.0,
        wheel_radius: 0.34,
        axle_z: 1.42,
        axle_x: 0.86,

        friction_slip: 1.15,
        side_friction_stiffness: 0.80,
        handbrake_side_drop: 0.10,
        handbrake_turn_gain: 0.0,

        engine_force: 15000.0,
        max_speed: 58.0,
        brake_force: 9000.0,
        handbrake_force: 6000.0,
        reverse_force: 5000.0,

        steer_max: 0.50,
        steer_rate: 3.2,
        steer_return: 4.6,
        steer_speed_falloff: 0.62,

        yaw_assist: 0.0,
        yaw_damp: 0.0,
        downforce: 3.0,
        lateral_bite: 0.0,
        air_righting: 1200.0,
    };

    /// «Аркада»: жёсткая подвеска, цепкая резина, доворот по рулю.
    pub const ARCADE: CarTuning = CarTuning {
        suspension_stiffness: 55.0,
        suspension_compression: 3.6,
        suspension_damping: 4.4,
        max_suspension_travel: 0.13,
        suspension_rest: 0.30,

        friction_slip: 2.8,
        side_friction_stiffness: 1.45,
        handbrake_side_drop: 0.045,
        handbrake_turn_gain: 0.35,

        engine_force: 21000.0,
        max_speed: 58.0,
        brake_force: 16000.0,
        handbrake_force: 9000.0,
        reverse_force: 7000.0,

        steer_max: 0.58,
        steer_rate: 6.5,
        steer_return: 9.0,
        steer_speed_falloff: 0.46,

        yaw_assist: 9000.0,
        yaw_damp: 2600.0,
        downforce: 11.0,
        lateral_bite: 0.05,
        air_righting: 4000.0,
        ..CarTuning::SIM
    };
}

/// Одна машина: тело, контроллер, состояние руля и заноса.
pub struct Car {
    pub body: RigidBodyHandle,
    pub controller: DynamicRayCastVehicleController,
    pub tuning: CarTuning,
    /// текущее положение руля, -1..1
    pub steer: f32,
    /// угол, реально отданный передним колёсам, рад
    pub steer_angle: f32,
    /// заряд заноса, с
    pub drift_charge: f32,
    /// сторона заноса
    pub drift_dir: f32,
}

pub struct World {
    pub bodies: RigidBodySet,
    pub colliders: ColliderSet,
    pub islands: IslandManager,
    pub broad_phase: BroadPhaseBvh,
    pub narrow_phase: NarrowPhase,
    pub impulse_joints: ImpulseJointSet,
    pub multibody_joints: MultibodyJointSet,
    pub ccd_solver: CCDSolver,
    pub pipeline: PhysicsPipeline,
    pub params: IntegrationParameters,
    pub gravity: Vector,
    pub cars: Vec<Car>,
    pub preset: Preset,
    pub tick: u64,
    /// буфер вершин полотна, пока хозяин их заливает
    pub track_verts: Vec<Vector>,
    /// буфер треугольников полотна
    pub track_tris: Vec<[u32; 3]>,
    /// подвижные предметы: тело и его полуразмер (для рендера)
    pub props: Vec<(RigidBodyHandle, f32)>,
}

impl World {
    pub fn new(preset: Preset) -> Self {
        let mut params = IntegrationParameters::default();
        params.dt = DT;
        // Больше итераций солвера — устойчивее подвеска на жёстких пресетах.
        params.num_solver_iterations = 8;
        World {
            bodies: RigidBodySet::new(),
            colliders: ColliderSet::new(),
            islands: IslandManager::new(),
            broad_phase: BroadPhaseBvh::new(),
            narrow_phase: NarrowPhase::new(),
            impulse_joints: ImpulseJointSet::new(),
            multibody_joints: MultibodyJointSet::new(),
            ccd_solver: CCDSolver::new(),
            pipeline: PhysicsPipeline::new(),
            params,
            gravity: Vector::new(0.0, -9.81, 0.0),
            cars: Vec::new(),
            preset,
            tick: 0,
            track_verts: Vec::new(),
            track_tris: Vec::new(),
            props: Vec::new(),
        }
    }

    /// Бесконечная ровная площадка — для площадки испытаний.
    pub fn add_ground_plane(&mut self, half_size: f32, friction: f32) {
        let col = ColliderBuilder::cuboid(half_size, 0.5, half_size)
            .translation(Vector::new(0.0, -0.5, 0.0))
            .friction(friction)
            .build();
        self.colliders.insert(col);
    }

    /// Коробка: конус, бордюр, стена, аппарель трамплина.
    pub fn add_box(
        &mut self,
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
        let rot = Rotation::from_rotation_y(yaw) * Rotation::from_rotation_x(pitch);
        let pose = Pose::from_parts(Vector::new(x, y, z), rot);
        if mass <= 0.0 {
            let col = ColliderBuilder::cuboid(hx, hy, hz)
                .position(pose)
                .friction(friction)
                .build();
            self.colliders.insert(col);
        } else {
            // Подвижный конус: сбиваем его и он улетает.
            let rb = RigidBodyBuilder::dynamic().position(pose).build();
            let h = self.bodies.insert(rb);
            let col = ColliderBuilder::cuboid(hx, hy, hz)
                .friction(friction)
                .density(mass / (8.0 * hx * hy * hz))
                .build();
            self.colliders.insert_with_parent(col, h, &mut self.bodies);
            if self.props.len() < MAX_PROPS {
                self.props.push((h, hx));
            }
        }
    }

    /// Полотно трассы одной треугольной сеткой.
    /// Возвращает 0 при успехе.
    pub fn commit_track(&mut self, friction: f32) -> u32 {
        if self.track_verts.is_empty() || self.track_tris.is_empty() {
            return 1;
        }
        let verts = core::mem::take(&mut self.track_verts);
        let tris = core::mem::take(&mut self.track_tris);
        // FIX_INTERNAL_EDGES снимает «зацепы» на швах между треугольниками —
        // без него машина спотыкается о рёбра полотна.
        let flags = TriMeshFlags::FIX_INTERNAL_EDGES;
        match ColliderBuilder::trimesh_with_flags(verts, tris, flags) {
            Ok(b) => {
                self.colliders.insert(b.friction(friction).build());
                0
            }
            Err(_) => 2,
        }
    }

    /// Ставит машину. Возвращает индекс или u32::MAX.
    pub fn spawn_car(&mut self, x: f32, y: f32, z: f32, yaw: f32, t: CarTuning) -> u32 {
        if self.cars.len() >= MAX_CARS {
            return u32::MAX;
        }

        let pose = Pose::from_parts(Vector::new(x, y, z), Rotation::from_rotation_y(yaw));
        let rb = RigidBodyBuilder::dynamic()
            .position(pose)
            // Демпфирование мизерное: гасит численный шум, не мешая езде.
            .linear_damping(0.02)
            .angular_damping(0.35)
            .can_sleep(false)
            .build();
        let body = self.bodies.insert(rb);

        // Инерция коробки считается руками: так центр масс можно опустить
        // вниз (иначе машина ложится на бок в первом же повороте), и так
        // числа не зависят от того, как Rapier выведет плотность.
        let m = t.mass;
        let ix = m / 3.0 * (t.half_height * t.half_height + t.half_length * t.half_length);
        let iy = m / 3.0 * (t.half_width * t.half_width + t.half_length * t.half_length);
        let iz = m / 3.0 * (t.half_width * t.half_width + t.half_height * t.half_height);
        let mprops = MassProperties::new(
            Vector::new(0.0, -t.com_drop, 0.0),
            m,
            Vector::new(ix, iy, iz),
        );
        let col = ColliderBuilder::cuboid(t.half_width, t.half_height, t.half_length)
            .mass_properties(mprops)
            // Кузов почти не трётся: за сцепление отвечают колёса.
            .friction(0.32)
            .restitution(0.1)
            .build();
        self.colliders
            .insert_with_parent(col, body, &mut self.bodies);

        let mut controller = DynamicRayCastVehicleController::new(body);
        // Нос машины смотрит в +Z (раздел 4 контракта), верх — +Y.
        controller.index_forward_axis = 2;
        controller.index_up_axis = 1;

        let tuning = WheelTuning {
            suspension_stiffness: t.suspension_stiffness,
            suspension_compression: t.suspension_compression,
            suspension_damping: t.suspension_damping,
            max_suspension_travel: t.max_suspension_travel,
            side_friction_stiffness: t.side_friction_stiffness,
            friction_slip: t.friction_slip,
            max_suspension_force: t.max_suspension_force,
        };
        let down = Vector::new(0.0, -1.0, 0.0);
        // Ось колеса. Контроллер считает forward = normal x axle, поэтому
        // ось надо давать ПРАВЫМ бортом. У нас +X — левый борт (раздел 4),
        // значит правый это -X. С +X машина едет задом наперёд.
        let axle = Vector::new(-1.0, 0.0, 0.0);
        // Порядок колёс: FL, FR, RL, RR.
        let pts = [
            Vector::new(t.axle_x, -t.half_height * 0.2, t.axle_z),
            Vector::new(-t.axle_x, -t.half_height * 0.2, t.axle_z),
            Vector::new(t.axle_x, -t.half_height * 0.2, -t.axle_z),
            Vector::new(-t.axle_x, -t.half_height * 0.2, -t.axle_z),
        ];
        for p in pts.iter() {
            controller.add_wheel(*p, down, axle, t.suspension_rest, t.wheel_radius, &tuning);
        }

        let idx = self.cars.len() as u32;
        // Размеры уезжают в общую память: рендеру нужно чем-то ставить колёса.
        unsafe {
            DESCS[idx as usize] = CarDesc {
                half_width: t.half_width,
                half_height: t.half_height,
                half_length: t.half_length,
                wheel_radius: t.wheel_radius,
                axle_x: t.axle_x,
                axle_z: t.axle_z,
                connection_y: -t.half_height * 0.2,
                suspension_rest: t.suspension_rest,
            };
        }
        self.cars.push(Car {
            body,
            controller,
            tuning: t,
            steer: 0.0,
            steer_angle: 0.0,
            drift_charge: 0.0,
            drift_dir: 0.0,
        });
        idx
    }

    /// Снять состояние машины в слот сохранения.
    pub fn save_car(&self, idx: usize) -> u32 {
        if idx >= self.cars.len() {
            return 1;
        }
        let car = &self.cars[idx];
        let b = &self.bodies[car.body];
        let p = b.position();
        let v = b.linvel();
        let a = b.angvel();
        let mut s = CarSave {
            px: p.translation.x,
            py: p.translation.y,
            pz: p.translation.z,
            qx: p.rotation.x,
            qy: p.rotation.y,
            qz: p.rotation.z,
            qw: p.rotation.w,
            vx: v.x,
            vy: v.y,
            vz: v.z,
            wx: a.x,
            wy: a.y,
            wz: a.z,
            steer: car.steer,
            steer_angle: car.steer_angle,
            drift_charge: car.drift_charge,
            drift_dir: car.drift_dir,
            wheel_rot: [0.0; 4],
            _pad: [0.0; 3],
        };
        for (i, w) in car.controller.wheels().iter().enumerate() {
            s.wheel_rot[i] = w.rotation;
        }
        unsafe {
            SAVES[idx] = s;
        }
        0
    }

    /// Вернуть машину в состояние из слота сохранения.
    pub fn restore_car(&mut self, idx: usize) -> u32 {
        if idx >= self.cars.len() {
            return 1;
        }
        let s = unsafe { SAVES[idx] };
        let car = &mut self.cars[idx];
        car.steer = s.steer;
        car.steer_angle = s.steer_angle;
        car.drift_charge = s.drift_charge;
        car.drift_dir = s.drift_dir;
        for (i, w) in car.controller.wheels_mut().iter_mut().enumerate() {
            w.rotation = s.wheel_rot[i];
        }
        let b = &mut self.bodies[car.body];
        b.set_position(
            Pose::from_parts(
                Vector::new(s.px, s.py, s.pz),
                Rotation::from_xyzw(s.qx, s.qy, s.qz, s.qw),
            ),
            true,
        );
        b.set_linvel(Vector::new(s.vx, s.vy, s.vz), true);
        b.set_angvel(Vector::new(s.wx, s.wy, s.wz), true);
        b.reset_forces(false);
        b.reset_torques(false);
        0
    }

    /// Один шаг: вводы -> контроллеры -> Rapier -> состояния.
    pub fn step(&mut self, inputs: &[CarInput], outputs: &mut [CarOut]) {
        let n = self.cars.len();

        // 1. Руль, тяга, тормоза — по вводу.
        for i in 0..n {
            let inp = inputs[i];
            let car = &mut self.cars[i];
            let t = car.tuning;

            let body = &self.bodies[car.body];
            let pose = *body.position();
            let linvel = body.linvel();
            let fwd = pose.rotation * Vector::new(0.0, 0.0, 1.0);
            let v_fwd = linvel.dot(fwd);
            let speed = linvel.length();

            // Руль подходит к цели с ограниченной скоростью — иначе
            // на клавиатуре машина дёргается.
            let target = inp.steer.clamp(-1.0, 1.0);
            let rate = if target.abs() > 1e-4 {
                t.steer_rate
            } else {
                t.steer_return
            };
            let d = target - car.steer;
            let max_d = rate * DT;
            car.steer += d.clamp(-max_d, max_d);

            // На скорости руль ужимается, иначе машину не удержать.
            let sp = (speed / t.max_speed).clamp(0.0, 1.0);
            let steer_angle = car.steer * t.steer_max * (1.0 - t.steer_speed_falloff * sp);

            // Тяга падает по той же формуле, что и в разделе 6.2 контракта.
            let throttle = inp.throttle.clamp(0.0, 1.0);
            let brake_in = inp.brake.clamp(0.0, 1.0);
            let hb = inp.handbrake.clamp(0.0, 1.0);

            let mut engine = 0.0;
            let mut brake = 0.0;
            if throttle > 0.0 {
                let fade = (1.0 - v_fwd / t.max_speed).max(0.0);
                engine = t.engine_force * throttle * fade;
            }
            if brake_in > 0.0 {
                if v_fwd > 0.5 {
                    brake = t.brake_force * brake_in;
                } else {
                    // Задний ход той же формулой.
                    let fade = (1.0 + v_fwd / 12.0).max(0.0);
                    engine = -t.reverse_force * brake_in * fade;
                }
            }
            let hb_brake = t.handbrake_force * hb;

            let wheels = car.controller.wheels_mut();
            // Передние — рулят, задние — тянут и держат ручник.
            wheels[0].steering = steer_angle;
            wheels[1].steering = steer_angle;
            wheels[2].steering = 0.0;
            wheels[3].steering = 0.0;
            for w in 0..4 {
                // Полный привод: ровнее на аркаде, проще в настройке.
                wheels[w].engine_force = engine * 0.25;
                // ВНИМАНИЕ: в контроллере `brake` — это потолок ИМПУЛЬСА (Н·с),
                // а `engine_force` — сила (Н), её контроллер сам умножит на dt.
                // Несогласованность унаследована от Bullet. Без множителя DT
                // тормозной путь со ста километров выходит метр с небольшим.
                wheels[w].brake = brake * 0.25 * DT;
            }
            wheels[2].brake += hb_brake * 0.5 * DT;
            wheels[3].brake += hb_brake * 0.5 * DT;
            // Ручник роняет боковое сцепление задней оси — отсюда занос.
            let base_side = t.side_friction_stiffness;
            let dropped = base_side * (1.0 - (1.0 - t.handbrake_side_drop) * hb);
            wheels[2].side_friction_stiffness = dropped;
            wheels[3].side_friction_stiffness = dropped;
            wheels[0].side_friction_stiffness = base_side;
            wheels[1].side_friction_stiffness = base_side;
            car.steer_angle = steer_angle;
        }

        // 2. Контроллеры колёс. Каждому нужен свой заём query pipeline,
        //    поэтому цикл отдельный.
        for i in 0..n {
            let car = &mut self.cars[i];
            let filter = QueryFilter::new().exclude_rigid_body(car.body);
            let queries = self.broad_phase.as_query_pipeline_mut(
                self.narrow_phase.query_dispatcher(),
                &mut self.bodies,
                &mut self.colliders,
                filter,
            );
            car.controller.update_vehicle(DT, queries);
        }

        // 3. Аркадная надстройка поверх твёрдого тела.
        for i in 0..n {
            let hb_in = inputs[i].handbrake.clamp(0.0, 1.0);
            let car = &mut self.cars[i];
            let t = car.tuning;
            let on_ground = car
                .controller
                .wheels()
                .iter()
                .filter(|w| w.raycast_info().is_in_contact)
                .count();

            let body = &mut self.bodies[car.body];
            // Rapier НЕ обнуляет силы после шага: add_force копится, пока его
            // не сбросят. Без этих двух строк прижим за две секунды
            // складывает подвеску и кладёт кузов на асфальт.
            body.reset_forces(false);
            body.reset_torques(false);
            let pose = *body.position();
            let linvel = body.linvel();
            let angvel = body.angvel();
            let fwd = pose.rotation * Vector::new(0.0, 0.0, 1.0);
            let left = pose.rotation * Vector::new(1.0, 0.0, 0.0);
            let up = pose.rotation * Vector::new(0.0, 1.0, 0.0);
            let v_fwd = linvel.dot(fwd);
            let v_lat = linvel.dot(left);

            if on_ground > 0 {
                // Прижим: держит машину на трамплине и в быстрых дугах.
                let sp2 = linvel.length_squared();
                body.add_force(Vector::new(0.0, -t.downforce * sp2, 0.0), true);

                if t.yaw_assist > 0.0 {
                    // Помощь в руле: доворачиваем кузов к тому курсу, который
                    // просит руль. Это и есть «аркадный» характер. Считаем по
                    // ТОМУ ЖЕ углу, что уехал в колёса (с поправкой на скорость),
                    // иначе помощь спорит с рулём.
                    let wheelbase = t.axle_z * 2.0;
                    let mut want = if wheelbase > 1e-3 {
                        v_fwd * car.steer_angle.tan() / wheelbase
                    } else {
                        0.0
                    };
                    // Потолок по сцеплению: просить больше, чем держит резина,
                    // нельзя — иначе машина ездит по рельсам и это видно.
                    let a_lat_max = 9.81 * t.friction_slip;
                    let w_max = a_lat_max / v_fwd.abs().max(4.0);
                    want = want.clamp(-w_max, w_max);
                    // Под ручником просим повернуть круче — это тот же смысл,
                    // что HANDBRAKE_TURN_GAIN из раздела 6.2 контракта.
                    want *= 1.0 + t.handbrake_turn_gain * hb_in;
                    let err = want - angvel.y;
                    let grip = (on_ground as f32) * 0.25;
                    body.add_torque(Vector::new(0.0, err * t.yaw_assist * grip, 0.0), true);
                }
                if t.yaw_damp > 0.0 {
                    // Гасим рыскание вокруг вертикали. Под ручником гасим
                    // заметно слабее, иначе занос не начинается вовсе.
                    let k = 1.0 - 0.75 * hb_in;
                    body.add_torque(
                        Vector::new(0.0, -angvel.y * t.yaw_damp * 0.15 * k, 0.0),
                        true,
                    );
                }
                if t.lateral_bite > 0.0 && hb_in < 0.5 {
                    // Прямое подъедание боковой скорости. Грубо, зато даёт
                    // ровно то «цепкое» ощущение, которого от Rapier одного
                    // добиться не выходит. Под ручником выключено — иначе
                    // оно съедает занос быстрее, чем тот успевает начаться.
                    let kill = v_lat * t.lateral_bite;
                    let imp = left * (-kill * body.mass());
                    body.apply_impulse(imp, true);
                }
            } else if t.air_righting > 0.0 {
                // В воздухе выравниваем кузов, чтобы после трамплина
                // машина приземлялась на колёса, а не на крышу.
                let tilt = up.cross(Vector::new(0.0, 1.0, 0.0));
                body.add_torque(tilt * t.air_righting - angvel * (t.air_righting * 0.12), true);
            }
        }

        // 4. Шаг Rapier.
        self.pipeline.step(
            self.gravity,
            &self.params,
            &mut self.islands,
            &mut self.broad_phase,
            &mut self.narrow_phase,
            &mut self.bodies,
            &mut self.colliders,
            &mut self.impulse_joints,
            &mut self.multibody_joints,
            &mut self.ccd_solver,
            &(),
            &(),
        );

        // 5. Заряд заноса и выгрузка состояния.
        for i in 0..n {
            let car = &mut self.cars[i];
            let body = &self.bodies[car.body];
            let pose = *body.position();
            let linvel = body.linvel();
            let angvel = body.angvel();
            let fwd = pose.rotation * Vector::new(0.0, 0.0, 1.0);
            let left = pose.rotation * Vector::new(1.0, 0.0, 0.0);
            let v_fwd = linvel.dot(fwd);
            let v_lat = linvel.dot(left);
            let speed_h = (linvel.x * linvel.x + linvel.z * linvel.z).sqrt();

            // Тот же смысл, что в 6.3: копим только за настоящий занос,
            // смена стороны обнуляет копилку.
            let slip = if v_fwd.abs() > 1.0 {
                v_lat / v_fwd.abs()
            } else {
                0.0
            };
            let hb = inputs[i].handbrake > 0.5;
            if hb && v_fwd > 7.0 && slip.abs() > 0.12 {
                let dir = if slip > 0.0 { 1.0 } else { -1.0 };
                if car.drift_dir != 0.0 && dir != car.drift_dir && slip.abs() > 0.06 {
                    car.drift_charge = 0.0;
                }
                car.drift_dir = dir;
                let k = ((v_fwd - 7.0) / (16.0 - 7.0)).clamp(0.0, 1.0);
                car.drift_charge += k * k * DT;
            } else if !hb {
                car.drift_dir = 0.0;
                car.drift_charge = 0.0;
            }

            let o = &mut outputs[i];
            let p = pose.translation;
            let q = pose.rotation;
            o.px = p.x;
            o.py = p.y;
            o.pz = p.z;
            // yaw по разделу 4: ноль — нос в +Z.
            o.yaw = fwd.x.atan2(fwd.z);
            o.qx = q.x;
            o.qy = q.y;
            o.qz = q.z;
            o.qw = q.w;
            o.vx = linvel.x;
            o.vy = linvel.y;
            o.vz = linvel.z;
            o.speed = speed_h;
            o.wx = angvel.x;
            o.wy = angvel.y;
            o.wz = angvel.z;
            o.slip_angle = if speed_h > 0.5 {
                (-v_lat).atan2(v_fwd.abs().max(0.001))
            } else {
                0.0
            };
            // Условные обороты: по скорости качения ведущих колёс.
            let rpm = (v_fwd.abs() / car.tuning.wheel_radius) * 9.5493 * 4.2;
            o.engine_rpm = rpm.clamp(800.0, 8000.0);
            o.drift_charge = car.drift_charge;
            o.drift_dir = car.drift_dir;

            let mut grounded = 0.0;
            for (wi, w) in car.controller.wheels().iter().enumerate() {
                let info = w.raycast_info();
                let wo = &mut o.wheels[wi];
                wo.suspension_length = info.suspension_length;
                wo.suspension_travel = (w.suspension_rest_length - info.suspension_length)
                    .clamp(-w.max_suspension_travel, w.max_suspension_travel);
                wo.suspension_force = w.wheel_suspension_force;
                wo.steering = w.steering;
                wo.rotation = w.rotation;
                wo.forward_impulse = w.forward_impulse;
                wo.side_impulse = w.side_impulse;
                wo.contact = if info.is_in_contact {
                    grounded += 1.0;
                    1.0
                } else {
                    0.0
                };
            }
            o.wheels_on_ground = grounded;
        }

        // 6. Позы подвижных предметов — для рендера.
        for (k, (h, hx)) in self.props.iter().enumerate() {
            let b = &self.bodies[*h];
            let p = b.position();
            unsafe {
                PROPS[k] = PropOut {
                    px: p.translation.x,
                    py: p.translation.y,
                    pz: p.translation.z,
                    hx: *hx,
                    qx: p.rotation.x,
                    qy: p.rotation.y,
                    qz: p.rotation.z,
                    qw: p.rotation.w,
                };
            }
        }

        self.tick += 1;
    }
}
