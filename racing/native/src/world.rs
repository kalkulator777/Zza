// Мир Rapier: полотно трассы треугольной сеткой, препятствия кубами,
// машины на встроенном DynamicRayCastVehicleController плюс аркадная
// надстройка поверх твёрдого тела.

use rapier3d::control::{DynamicRayCastVehicleController, WheelTuning};
use rapier3d::prelude::*;

use crate::abi::{CarDesc, CarEffect, CarInput, CarOut, CarSave, PropOut, DESCS, MAX_CARS,
                 MAX_PROPS, PROPS, SAVES};

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
/// Сама структура описана в tools/abi_layout.json и выпущена генератором
/// в `abi_gen.rs`: её раскладка `repr(C)` из одних f32 — часть ABI, хозяин
/// видит её как плоский массив чисел в общей памяти и правит напрямую,
/// без единого дополнительного вызова. Так «аркада» и «симулятор»
/// становятся данными, а не кодом. Здесь — только сами пресеты.
pub use crate::abi::CarTuning;

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

        // Награда за занос (6.3) в пресете НУЛЕВАЯ, и это не забывчивость.
        // Пороги и длительности — игровые константы раздела 6, а модуль
        // игровых констант не знает (12.21): их кладёт хозяин из
        // game/physics.py и static/js/physics.js. Ноль значит «уровня нет»,
        // поэтому модуль без хозяйских чисел просто не платит.
        drift_charge_l1: 0.0,
        drift_charge_l2: 0.0,
        drift_charge_l3: 0.0,
        drift_boost_l1: 0.0,
        drift_boost_l2: 0.0,
        drift_boost_l3: 0.0,
        boost_speed: 0.0,
        boost_accel: 0.0,
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
    /// остаток ускорения за занос, с
    pub boost_time: f32,
    /// уровень, выплаченный на этом шаге (0..3); живёт один шаг
    pub drift_level: f32,
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
            let rb = RigidBodyBuilder::dynamic().pose(pose).build();
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
    /// Ставит полотно трассы треугольной сеткой. Вершины и треугольники
    /// считает сам модуль (см. trackmesh.rs), сюда приходят готовые срезы.
    /// 0 — успех, 1 — пустая сетка, 2 — Rapier не принял.
    pub fn commit_track(&mut self, verts: &[f32], tris: &[u32], friction: f32) -> u32 {
        if verts.len() < 9 || tris.len() < 3 {
            return 1;
        }
        let pts: Vec<Vector> = verts
            .chunks_exact(3)
            .map(|c| Vector::new(c[0], c[1], c[2]))
            .collect();
        let idx: Vec<[u32; 3]> = tris.chunks_exact(3).map(|c| [c[0], c[1], c[2]]).collect();
        // FIX_INTERNAL_EDGES снимает «зацепы» на швах между треугольниками —
        // без него машина спотыкается о рёбра полотна.
        let flags = TriMeshFlags::FIX_INTERNAL_EDGES;
        match ColliderBuilder::trimesh_with_flags(pts, idx, flags) {
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
            .pose(pose)
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
            boost_time: 0.0,
            drift_level: 0.0,
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
            boost_time: car.boost_time,
            wheel_rot: [0.0; 4],
            _pad: [0.0; 2],
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
        car.boost_time = s.boost_time;
        // Выплата держится один шаг и в слот не пишется: переигранный шаг
        // выставит её заново там же, где она случилась в первый раз.
        car.drift_level = 0.0;
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
    ///
    /// `effects` — внешние воздействия на этот шаг (бонусы, покрытие,
    /// толчки). Доза живёт РОВНО ОДИН шаг: в конце шага записи обнуляются,
    /// и хозяин пишет их заново каждый тик, пока действие длится. Нулевая
    /// запись обязана считаться бит в бит так же, как считалось до
    /// появления `CarEffect` — на этом стоят все снятые хэши.
    pub fn step(&mut self, inputs: &[CarInput], outputs: &mut [CarOut],
                effects: &mut [CarEffect]) {
        let n = self.cars.len();

        // 1. Руль, тяга, тормоза — по вводу.
        for i in 0..n {
            let eff = effects[i];
            // Ввод заглушён: шаг 1 раздела 6.2. Не «руль в ноль мгновенно»,
            // а «кнопки отпущены» — руль возвращается своим steer_return,
            // ровно как у классики с обнулёнными битами кнопок. Флаг
            // offtrack кнопкой не является и глушению не подлежит.
            let mut inp = inputs[i];
            if eff.stun > 0.5 {
                inp.throttle = 0.0;
                inp.brake = 0.0;
                inp.steer = 0.0;
                inp.handbrake = 0.0;
            }
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
            // Покрытие под машиной: масло и обломки съедают долю сцепления.
            // При нулевой дозе множитель ровно 1.0, то есть в колёса
            // записываются те же биты, что стояли там со spawn_car, и хэш
            // мира не шевелится.
            let grip = 1.0 - eff.grip_drop.clamp(0.0, 1.0);
            for w in 0..4 {
                wheels[w].friction_slip = t.friction_slip * grip;
            }
            // Ручник роняет боковое сцепление задней оси — отсюда занос.
            let base_side = t.side_friction_stiffness * grip;
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
            let eff = effects[i];
            let stunned = eff.stun > 0.5;
            let hb_in = if stunned {
                0.0
            } else {
                inputs[i].handbrake.clamp(0.0, 1.0)
            };
            // Покрытие: та же доля, что в проходе 1 ушла в колёса. Всё,
            // чем аркадная надстройка подменяет резину, обязано ронять
            // сцепление вместе с ней — иначе на масле машину несёт, а
            // помощь в руле продолжает доворачивать её как по сухому.
            let grip = 1.0 - eff.grip_drop.clamp(0.0, 1.0);
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

            // Воздействия, которые ставят состояние машины, — до всего
            // остального в этом проходе: доза относится К ЭТОМУ шагу, и
            // выданный буст обязан тянуть уже сейчас, а не со следующего.
            //
            // Буст: правило то же, что у награды за занос (§12.24) — новый
            // не укорачивает уже идущий. Оно же закрывает и «применить
            // дважды»: две дозы подряд не складываются, берётся большая.
            if eff.boost_add > car.boost_time {
                car.boost_time = eff.boost_add;
            }
            if eff.drift_reset > 0.5 {
                // Копилка сгорает БЕЗ выплаты. Классика пишет это руками в
                // items._apply_hit; здесь то же место — до шага 15, который
                // иначе увидел бы отпущенный (заглушённый) ручник и заплатил
                // бы за занос тому, кого только что сбили.
                car.drift_charge = 0.0;
                car.drift_dir = 0.0;
            }
            if eff.shift_x != 0.0 || eff.shift_y != 0.0 || eff.shift_z != 0.0 {
                // Сдвиг позиции до шага солвера: остаток перекрытия он
                // доразведёт сам. Поворот и скорости не трогаются.
                let p = *body.position();
                body.set_position(
                    Pose::from_parts(
                        Vector::new(
                            p.translation.x + eff.shift_x,
                            p.translation.y + eff.shift_y,
                            p.translation.z + eff.shift_z,
                        ),
                        p.rotation,
                    ),
                    true,
                );
            }
            if eff.push_x != 0.0 || eff.push_y != 0.0 || eff.push_z != 0.0 {
                // Толчок задан ПРИРАЩЕНИЕМ СКОРОСТИ: число не зависит от
                // массы машины и читается так же, как state.vx у классики.
                let m = body.mass();
                body.apply_impulse(
                    Vector::new(eff.push_x * m, eff.push_y * m, eff.push_z * m),
                    true,
                );
            }
            if eff.spin_rate != 0.0 {
                // Раскрутка после попадания. Курс доворачивается
                // КИНЕМАТИЧЕСКИ, как у классики (yaw += SPIN_RATE*dt):
                // скорость не трогаем, машину разворачивает поперёк
                // собственного хода, а дальше её стаскивает боковое
                // сцепление — ровно та же картинка, что в старой физике.
                // q' = rotY(Δ)·q — доворот вокруг МИРОВОЙ вертикали.
                let p = *body.position();
                let turn = Rotation::from_rotation_y(eff.spin_rate * DT);
                body.set_position(
                    Pose::from_parts(p.translation, turn * p.rotation),
                    true,
                );
                // Своей угловой скорости у раскрутки нет: иначе она
                // складывалась бы с кинематическим доворотом и машина
                // крутилась бы вдвое быстрее заказанного.
                let a = body.angvel();
                body.set_angvel(Vector::new(a.x, 0.0, a.z), true);
            }

            // Поза и скорости читаются ПОСЛЕ воздействий: сдвиг, толчок и
            // доворот относятся к этому шагу, и всё, что ниже, обязано
            // видеть уже их результат.
            let pose = *body.position();
            let linvel = body.linvel();
            let angvel = body.angvel();
            let fwd = pose.rotation * Vector::new(0.0, 0.0, 1.0);
            let left = pose.rotation * Vector::new(1.0, 0.0, 0.0);
            let up = pose.rotation * Vector::new(0.0, 1.0, 0.0);
            let v_fwd = linvel.dot(fwd);
            let v_lat = linvel.dot(left);

            // Ускорение за занос — шаг 7 раздела 6.2, слово в слово: тянем
            // продольную скорость к boost_speed с ускорением boost_accel,
            // с ОБЕИХ сторон (буст и придерживает того, кто уже быстрее).
            // В воздухе не тянет — колёса не на земле, — но таймер тикает:
            // иначе прыжок стал бы способом придержать буст до удобного
            // места, дыра ровно того сорта, что закрыта в 12.15.
            if car.boost_time > 0.0 {
                if on_ground > 0 && t.boost_accel > 0.0 {
                    let delta = t.boost_speed - v_fwd;
                    let step = t.boost_accel * DT;
                    let dv = if delta > step {
                        step
                    } else if delta < -step {
                        -step
                    } else {
                        delta
                    };
                    body.apply_impulse(fwd * (dv * body.mass()), true);
                }
                car.boost_time -= DT;
                if car.boost_time < 0.0 {
                    car.boost_time = 0.0;
                }
            }

            // Замедление — шаг 8 раздела 6.2 тем же приёмом: снимаем долю
            // ПРОДОЛЬНОЙ скорости. Одно поле на «Грозу» и на тряску по
            // обломкам. Симметрично бусту: в воздухе не тормозит (тормозить
            // нечем), а таймер тикает у хозяина и в воздухе тоже.
            if eff.speed_drop > 0.0 && on_ground > 0 {
                let k = eff.speed_drop.clamp(0.0, 1.0);
                body.apply_impulse(fwd * (-v_fwd * k * body.mass()), true);
            }

            if on_ground > 0 {
                // Прижим: держит машину на трамплине и в быстрых дугах.
                let sp2 = linvel.length_squared();
                body.add_force(Vector::new(0.0, -t.downforce * sp2, 0.0), true);

                // Раскрутка спорит с помощью в руле и с гашением рыскания:
                // обе тянут кузов к «правильному» курсу, а раскрутка — это
                // ровно потеря курса. Пока крутит, аркадная надстройка
                // молчит; у классики её роль играли обнулённые кнопки.
                let spinning = eff.spin_rate != 0.0;
                if t.yaw_assist > 0.0 && !spinning {
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
                    let a_lat_max = 9.81 * t.friction_slip * grip;
                    let w_max = a_lat_max / v_fwd.abs().max(4.0);
                    want = want.clamp(-w_max, w_max);
                    // Под ручником просим повернуть круче — это тот же смысл,
                    // что HANDBRAKE_TURN_GAIN из раздела 6.2 контракта.
                    want *= 1.0 + t.handbrake_turn_gain * hb_in;
                    let err = want - angvel.y;
                    let grip = (on_ground as f32) * 0.25;
                    body.add_torque(Vector::new(0.0, err * t.yaw_assist * grip, 0.0), true);
                }
                if t.yaw_damp > 0.0 && !spinning {
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
                    let kill = v_lat * t.lateral_bite * grip;
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
            // Заглушённый ввод не копит занос: у классики шаг 15 смотрит на
            // те же обнулённые биты кнопок, что и шаги 1–13.
            let hb = effects[i].stun <= 0.5 && inputs[i].handbrake > 0.5;
            // Третий барьер 12.15: за занос ВНЕ полотна заряд не копится.
            // Полотна модуль не знает, флаг приходит от хозяина (CarInput).
            let offtrack = inputs[i].offtrack > 0.5;
            let wheels_down = car
                .controller
                .wheels()
                .iter()
                .any(|w| w.raycast_info().is_in_contact);
            car.drift_level = 0.0;
            // Три ветки — те же, что в шаге 15 раздела 6.2, и в том же
            // порядке: сбит, копим, выплачиваем.
            if !wheels_down {
                // Оторвало от земли — занос сбит, копилка сгорает БЕЗ
                // награды. Четвёртый барьер из 12.15: иначе трамплин стал бы
                // способом обналичить заряд там, где за занос не платят.
                car.drift_dir = 0.0;
                car.drift_charge = 0.0;
            } else if hb && v_fwd > 7.0 {
                if slip.abs() > 0.12 {
                    let dir = if slip > 0.0 { 1.0 } else { -1.0 };
                    if car.drift_dir != 0.0 && dir != car.drift_dir && slip.abs() > 0.06 {
                        car.drift_charge = 0.0;
                    }
                    car.drift_dir = dir;
                    // Вне полотна заряд не копится, но и не сгорает —
                    // ровно как в классике: там это один флаг в условии.
                    if !offtrack {
                        let k = ((v_fwd - 7.0) / (16.0 - 7.0)).clamp(0.0, 1.0);
                        car.drift_charge += k * k * DT;
                    }
                }
            } else {
                // Ручник отпущен или скорость потеряна — копилка
                // выплачивается. Пороги и длительности приходят от хозяина;
                // ноль порога значит «такого уровня нет».
                let t = car.tuning;
                let charge = car.drift_charge;
                let (level, reward) = if t.drift_charge_l3 > 0.0 && charge >= t.drift_charge_l3 {
                    (3.0, t.drift_boost_l3)
                } else if t.drift_charge_l2 > 0.0 && charge >= t.drift_charge_l2 {
                    (2.0, t.drift_boost_l2)
                } else if t.drift_charge_l1 > 0.0 && charge >= t.drift_charge_l1 {
                    (1.0, t.drift_boost_l1)
                } else {
                    (0.0, 0.0)
                };
                // Новый буст не укорачивает уже идущий — как в 6.3.
                if reward > car.boost_time {
                    car.boost_time = reward;
                }
                car.drift_level = level;
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
            o.boost_time = car.boost_time;
            o.drift_level = car.drift_level;

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

        // 7. Дозы воздействий сгорают. Это и есть то, что делает их
        //    безопасными: хозяин, забывший стереть своё воздействие, не
        //    получит вечную раскрутку, а переигранный при откате тик
        //    получит ровно ту дозу, которую хозяин выпишет заново из
        //    своего таймера — а не ту, что осталась в памяти модуля.
        for e in effects.iter_mut().take(n) {
            *e = CarEffect::INIT;
        }

        self.tick += 1;
    }
}
