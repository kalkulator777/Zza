// Шаг физики машины — чистая функция, без знания о сети и трассе.
//
// ЗЕРКАЛО: shared/carstep.py — правки повторять один-в-один.
// tools/crosscheck.py прогоняет одинаковые вводы через обе версии и требует
// ПОБИТОВОГО совпадения траекторий. Если оно падает — это баг, а не шум.
//
// Правило: только + - * / sqrt и свои fsin/fcos. Никаких Math.sin, atan2,
// hypot, ** — они не гарантируют одинаковый результат в Python и JS.

import { fsin, fcos, normAngle } from './fixmath.js';

// Биты ввода
export const IN_UP = 1;
export const IN_DOWN = 2;
export const IN_LEFT = 4;
export const IN_RIGHT = 8;
export const IN_HANDBRAKE = 16;
export const IN_USE = 32;

// Индексы покрытий. Порядок фиксирован и приходит с сервера вместе с трассой.
export const SURFACE_ORDER = ['road', 'grass', 'sand', 'ice', 'boost', 'wall'];

/**
 * Продвигает car на dt секунд.
 * car  — изменяемый объект с полями x, y, vx, vy, a
 * inp  — битовая маска ввода
 * surf — модификаторы покрытия [grip, drag, accel, maxSpeed]
 * C    — константы машины из physics.json
 * eff  — эффекты бонусов [accelMul, maxSpeedMul, steerMul, gripMul, spin, control]
 *        control: 1 обычно, -1 управление наоборот, 0 нет управления
 */
export function step(car, inp, dt, surf, C, eff) {
  const control = eff[5];

  let throttle, braking, steer;
  if (control === 0.0) {
    throttle = 0.0;
    braking = 0.0;
    steer = 0.0;
  } else {
    throttle = (inp & IN_UP) ? 1.0 : 0.0;
    braking = (inp & IN_DOWN) ? 1.0 : 0.0;
    steer = ((inp & IN_RIGHT) ? 1.0 : 0.0) - ((inp & IN_LEFT) ? 1.0 : 0.0);
    if (control < 0.0) {
      steer = -steer;
    }
  }

  // Ручник тоже отключается вместе с управлением: иначе зажатый Shift на
  // отсчёте давал бы клиенту физику, отличную от серверной.
  const handbrake = control === 0.0 ? 0.0 : ((inp & IN_HANDBRAKE) ? 1.0 : 0.0);

  const ca = fcos(car.a);
  const sa = fsin(car.a);

  // Раскладываем скорость на продольную и поперечную в ТЕКУЩЕМ базисе машины.
  let vf = car.vx * ca + car.vy * sa;
  let vl = -car.vx * sa + car.vy * ca;

  const maxSpeed = C.maxSpeed * surf[3] * eff[1];
  const accel = C.accel * surf[2] * eff[0];

  // Двигатель
  if (throttle > 0.0) {
    if (vf < maxSpeed) {
      vf = vf + accel * dt;
      if (vf > maxSpeed) {
        vf = maxSpeed;
      }
    }
  }

  // Тормоз, затем задний ход
  if (braking > 0.0) {
    if (vf > 0.0) {
      vf = vf - C.brake * dt;
      if (vf < 0.0) {
        vf = 0.0;
      }
    } else {
      vf = vf - C.reverseAccel * dt;
      const maxRev = -C.maxReverse;
      if (vf < maxRev) {
        vf = maxRev;
      }
    }
  }

  // Сопротивление. Коэффициент зажат в [0,1]: живая подстройка (F4) позволяет
  // выкрутить константы так, что явная схема разойдётся в осцилляцию.
  let kd = C.drag * surf[1] * dt;
  if (kd > 1.0) {
    kd = 1.0;
  } else if (kd < 0.0) {
    kd = 0.0;
  }
  vf = vf - vf * kd;

  // Боковое сцепление: чем меньше, тем сильнее занос.
  let grip;
  if (handbrake > 0.0) {
    grip = C.handbrakeGrip;
  } else {
    grip = C.grip;
  }
  let kg = grip * surf[0] * eff[3] * dt;
  if (kg > 1.0) {
    kg = 1.0;
  } else if (kg < 0.0) {
    kg = 0.0;
  }
  vl = vl - vl * kg;

  // Поворот: на месте не крутимся, на большой скорости руль тяжелеет.
  const av = vf >= 0.0 ? vf : -vf;
  let sf = av / C.steerFullSpeed;
  if (sf > 1.0) {
    sf = 1.0;
  }
  sf = sf / (1.0 + av * C.steerFalloff);

  let turn = C.steerRate * eff[2] * steer * sf;
  if (vf < 0.0) {
    turn = -turn;
  }
  if (handbrake > 0.0) {
    turn = turn * C.handbrakeSteerBonus;
  }

  // Скорость собираем обратно в СТАРОМ базисе — поперечная составляющая
  // остаётся в мировых координатах, это и есть занос.
  car.vx = vf * ca - vl * sa;
  car.vy = vf * sa + vl * ca;

  car.a = normAngle(car.a + turn * dt + eff[4] * dt);
  car.x = car.x + car.vx * dt;
  car.y = car.y + car.vy * dt;
}

export function noEffects() {
  return [1.0, 1.0, 1.0, 1.0, 0.0, 1.0];
}

/**
 * Один тик езды: покрытие под машиной -> физика -> стены.
 *
 * ЗЕРКАЛО: drive_tick в shared/carstep.py.
 *
 * Это единица, которую сервер выполняет для каждой машины, а клиент — только
 * для своей (предсказание). Столкновений машин здесь нет: их считает исключи-
 * тельно сервер, клиент их не предсказывает.
 *
 * Возвращает силу удара о стену (0 — не было).
 */
export function driveTick(car, inp, dt, track, C, CC, eff, surfaces) {
  let q = track.query(car.x, car.y, car.seg);
  car.seg = q[0];
  step(car, inp, dt, surfaces[q[4]], C, eff);

  q = track.query(car.x, car.y, car.seg);
  car.seg = q[0];
  return track.resolveWall(car, q[2], q[3], q[5], q[6], CC);
}
