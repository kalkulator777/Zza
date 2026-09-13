// Детерминированная математика.
//
// ЗЕРКАЛО: shared/fixmath.py — любая правка здесь должна быть повторена там
// один-в-один. tools/crosscheck.py проверяет побитовое совпадение.
//
// Зачем свои sin/cos вместо Math.sin: Math.sin в JS и sin из libm в C могут
// отличаться в последнем бите. Для клиентского предсказания это означало бы
// медленный дрейф, неотличимый от настоящего бага. Полином ниже использует
// только + - * / — а они по IEEE-754 корректно округляются одинаково в обоих
// языках, значит результат совпадает побитово.

export const PI = 3.141592653589793;
export const TWO_PI = 6.283185307179586;
export const HALF_PI = 1.5707963267948966;

// Коэффициенты ряда Тейлора для sin на [-pi/2, pi/2] (до x^13).
// Записаны десятичными литералами, чтобы оба языка разобрали их в один double.
const C3 = 0.16666666666666666;
const C5 = 0.008333333333333333;
const C7 = 0.0001984126984126984;
const C9 = 2.755731922398589e-06;
const C11 = 2.505210838544172e-08;
const C13 = 1.6059043836821613e-10;

/** Приводит угол к [-pi, pi). */
export function normAngle(a) {
  return a - TWO_PI * Math.floor((a + PI) / TWO_PI);
}

export function fsin(x) {
  x = normAngle(x);
  if (x > HALF_PI) {
    x = PI - x;
  } else if (x < -HALF_PI) {
    x = -PI - x;
  }
  const u = x * x;
  let p = C13 * u - C11;
  p = p * u + C9;
  p = p * u - C7;
  p = p * u + C5;
  p = p * u - C3;
  p = p * u + 1.0;
  return x * p;
}

export function fcos(x) {
  return fsin(x + HALF_PI);
}

export function flen(x, y) {
  return Math.sqrt(x * x + y * y);
}

export function clamp(v, lo, hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

export function sign(v) {
  if (v > 0.0) return 1.0;
  if (v < 0.0) return -1.0;
  return 0.0;
}
