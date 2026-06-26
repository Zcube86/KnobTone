/**
 * KnobTone Desktop Demo — ESP32-S3 固件
 * =========================================
 * 职责：
 *   1. 读取旋钮位置（AS5600 编码器）
 *   2. 控制力矩（TMC6300 驱动 BLDC 电机）
 *   3. 检测按压事件
 *   4. 通过 USB Serial 与 PC 通信（JSON 协议）
 */

#include <Arduino.h>
#include <SimpleFOC.h>
#include <ArduinoJson.h>

// ─── 引脚定义 ───────────────────────────────────────────────
// (根据实际接线修改)

// I2C — AS5600 编码器
#define I2C_SDA       4
#define I2C_SCL       5

// PWM — TMC6300 电机驱动
#define MOTOR_PWM_U   6
#define MOTOR_PWM_V   7
#define MOTOR_PWM_W   8
#define MOTOR_ENABLE  9

// 按压检测
#define BUTTON_PIN    10     // 旋钮按压开关（低电平有效）

// ─── 常量 ───────────────────────────────────────────────────
#define SERIAL_BAUD  115200
#define POLE_PAIRS   7       // BLDC 电机极对数（根据实际电机修改）
#define DETENT_WIDTH 0.05f   // Detent 槽宽度（弧度）
#define DETENT_STRENGTH 0.3f // Detent 力度

// ─── 对象 ───────────────────────────────────────────────────

// 电机
BLDCMotor motor = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM driver = BLDCDriver3PWM(
    MOTOR_PWM_U, MOTOR_PWM_V, MOTOR_PWM_W, MOTOR_ENABLE
);

// 编码器
MagneticSensorI2C sensor = MagneticSensorI2C(AS5600_I2C);

// 串口 JSON
JsonDocument json_rx;  // 接收缓冲区

// ─── 旋钮状态 ───────────────────────────────────────────────

// 当前模式
enum class KnobMode : uint8_t {
    VOLUME,
    TRACKLIST,
    SEEK,
    EQ
};

KnobMode current_mode = KnobMode::VOLUME;

// 位置参数
float target_position = 0.0f;     // 目标角度（rad）
float position_max = 2.0f * PI;   // 最大角度（默认一圈）
int step_count = 100;             // Detent 步数（音量 = 100 步/圈）
int current_step = 0;             // 当前步数

// 力反馈参数
float detent_strength = DETENT_STRENGTH;
bool smooth_mode = false;         // true = 快进模式（无 Detent）
float boundary_strength = 0.5f;   // 边界阻力

// 按压检测
bool button_pressed = false;
bool button_prev = false;
unsigned long press_start_ms = 0;
unsigned long press_duration_ms = 0;
bool press_released = false;
int click_count = 0;
unsigned long last_click_ms = 0;
const unsigned long MULTI_CLICK_TIMEOUT = 400;  // 连击超时（ms）
const unsigned long LONG_PRESS_THRESHOLD = 600; // 长按阈值（ms）

// ─── 初始化 ─────────────────────────────────────────────────

void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(1000);  // 等待 USB Serial 就绪

    // 按压引脚
    pinMode(BUTTON_PIN, INPUT_PULLUP);

    // I2C
    Wire.begin(I2C_SDA, I2C_SCL);

    // 编码器
    sensor.init();
    if (sensor.isInitSuccess()) {
        Serial.println("[OK] AS5600 编码器就绪");
    } else {
        Serial.println("[ERR] AS5600 初始化失败！");
    }

    // 电机驱动
    driver.voltage_power_supply = 5.0f;
    driver.voltage_limit = 3.0f;
    driver.init();
    motor.linkDriver(&driver);

    // 编码器关联电机
    motor.linkSensor(&sensor);

    // 电机参数
    motor.controller = MotionControlType::torque;
    motor.torque_controller = TorqueControlType::voltage;
    motor.voltage_limit = 2.0f;
    motor.velocity_limit = 20.0f;

    // PID（先用默认值）
    motor.PID_velocity.P = 0.5f;
    motor.PID_velocity.I = 2.0f;
    motor.PID_velocity.D = 0.0f;
    motor.LPF_velocity.Tf = 0.01f;

    motor.useMonitoring(Serial);
    motor.init();
    motor.initFOC();

    target_position = sensor.getAngle();
    current_step = angle_to_step(target_position);

    Serial.println("[OK] 固件就绪");
    Serial.println("{\"event\": \"ready\"}");
}

// ─── 工具函数 ───────────────────────────────────────────────

float angle_to_step(float angle) {
    // 将角度映射到步数
    if (step_count <= 0) return 0;
    float normalized = fmod(angle, 2.0f * PI);
    if (normalized < 0) normalized += 2.0f * PI;
    return round(normalized / (2.0f * PI) * step_count);
}

int angle_to_step_int(float angle) {
    int step = (int)angle_to_step(angle);
    return step % step_count;
}

// ─── 力反馈计算 ─────────────────────────────────────────────

float compute_torque(float current_angle) {
    /**
     * 根据当前模式计算力矩：
     * - VOLUME:  100 Detents/圈，每格有回中力
     * - TRACKLIST: 每个位置一个 Detent
     * - SEEK:    完全平滑，无 Detent
     * - EQ:      少数几个 Detent
     */
    if (smooth_mode) {
        // 快进模式 — 完全平滑，只加轻微阻尼
        float velocity = sensor.getVelocity();
        return -velocity * 0.01f;  // 微小阻尼
    }

    // Detent 计算
    float step_angle = (2.0f * PI) / step_count;
    float nearest_step_angle = round(current_angle / step_angle) * step_angle;
    float error = nearest_step_angle - current_angle;

    // 在 Detent 位置施加回中力（类似弹簧）
    float torque = error * detent_strength * 20.0f;

    // 边界阻力
    if (current_angle < 0.05f) {
        torque += boundary_strength * (0.05f - current_angle);
    } else if (current_angle > position_max - 0.05f) {
        torque -= boundary_strength * (current_angle - (position_max - 0.05f));
    }

    return constrain(torque, -2.0f, 2.0f);
}

// ─── 按压检测 ───────────────────────────────────────────────

void check_button() {
    bool current = !digitalRead(BUTTON_PIN);  // 低电平 = 按下

    if (current && !button_prev) {
        // 按下
        button_pressed = true;
        press_start_ms = millis();
        press_released = false;
    }

    if (!current && button_prev) {
        // 释放
        press_duration_ms = millis() - press_start_ms;
        press_released = true;
    }

    button_prev = current;
}

void process_button_events() {
    if (!press_released) return;
    press_released = false;

    unsigned long now = millis();

    if (press_duration_ms >= LONG_PRESS_THRESHOLD) {
        // 长按
        JsonDocument doc;
        doc["event"] = "long_press";
        doc["duration_ms"] = press_duration_ms;
        serializeJson(doc, Serial);
        Serial.println();
        click_count = 0;  // 重置连击计数
        return;
    }

    // 短按 → 累积连击
    click_count++;

    if (click_count == 1) {
        last_click_ms = now;
        // 等超时再判断是单击还是连击
        return;
    }

    // 连击超时检查在主循环里
}

void check_click_timeout() {
    if (click_count == 0) return;
    unsigned long now = millis();

    if (now - last_click_ms > MULTI_CLICK_TIMEOUT) {
        // 超时，根据连击次数发送事件
        JsonDocument doc;

        switch (click_count) {
            case 1:
                doc["event"] = "press";
                doc["duration_ms"] = press_duration_ms;
                break;
            case 2:
                doc["event"] = "double_click";
                break;
            case 3:
                doc["event"] = "triple_click";
                break;
            default:
                doc["event"] = "multi_click";
                doc["count"] = click_count;
                break;
        }

        serializeJson(doc, Serial);
        Serial.println();
        click_count = 0;
    }
}

// ─── 旋转检测 ───────────────────────────────────────────────

float last_angle = 0.0f;
float angle_accumulator = 0.0f;
const float ROTATE_THRESHOLD = 0.02f;  // 最小旋转角度（弧度）

void check_rotation() {
    float current_angle = sensor.getAngle();

    // 处理角度跨越 0/2π 的情况
    float delta = current_angle - last_angle;
    if (delta > PI) delta -= 2.0f * PI;
    if (delta < -PI) delta += 2.0f * PI;

    if (abs(delta) > ROTATE_THRESHOLD) {
        angle_accumulator += delta;

        // 累计足够步数后发送事件
        float step_size = (2.0f * PI) / step_count;
        int steps = (int)(angle_accumulator / step_size);

        if (steps != 0) {
            JsonDocument doc;
            doc["event"] = "rotate";
            doc["direction"] = (steps > 0) ? "cw" : "ccw";
            doc["steps"] = abs(steps);
            doc["position"] = angle_to_step_int(current_angle);
            serializeJson(doc, Serial);
            Serial.println();

            angle_accumulator -= steps * step_size;
        }

        target_position = current_angle;
    }

    last_angle = current_angle;
}

// ─── 命令处理 ───────────────────────────────────────────────

void process_command(const JsonDocument& doc) {
    const char* cmd = doc["cmd"];
    if (!cmd) return;

    if (strcmp(cmd, "mode") == 0) {
        // 切换模式
        const char* mode_str = doc["mode"];
        if (mode_str) {
            if (strcmp(mode_str, "volume") == 0) {
                current_mode = KnobMode::VOLUME;
                step_count = 100;
                detent_strength = DETENT_STRENGTH;
                smooth_mode = false;
                position_max = 2.0f * PI;
            } else if (strcmp(mode_str, "tracklist") == 0) {
                current_mode = KnobMode::TRACKLIST;
                step_count = doc["max"] | 10;
                detent_strength = DETENT_STRENGTH * 1.2f;
                smooth_mode = false;
                position_max = 2.0f * PI;
            } else if (strcmp(mode_str, "seek") == 0) {
                current_mode = KnobMode::SEEK;
                smooth_mode = true;
                position_max = 2.0f * PI;
            } else if (strcmp(mode_str, "eq") == 0) {
                current_mode = KnobMode::EQ;
                step_count = doc["max"] | 6;
                detent_strength = DETENT_STRENGTH;
                smooth_mode = false;
                position_max = 2.0f * PI;
            }
        }
    } else if (strcmp(cmd, "haptic") == 0) {
        // 瞬时触觉效果
        const char* type = doc["type"];
        if (type && strcmp(type, "click") == 0) {
            // 短振动（待实现：实际用电机力矩脉冲）
            motor.move(0.5f);  // 瞬时力矩
            delay(30);
            motor.move(0.0f);
        } else if (type && strcmp(type, "bump") == 0) {
            motor.move(1.0f);
            delay(50);
            motor.move(0.0f);
        }
    }
}

// ─── 主循环 ─────────────────────────────────────────────────

void loop() {
    // 1. 运行 FOC 算法
    motor.loopFOC();

    // 2. 力反馈力矩计算
    float current_angle = sensor.getAngle();
    float torque = compute_torque(current_angle);
    motor.move(torque);

    // 3. 检测旋转
    check_rotation();

    // 4. 检测按压
    check_button();
    process_button_events();
    check_click_timeout();

    // 5. 处理 PC 指令
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();
        if (line.length() > 0) {
            DeserializationError err = deserializeJson(json_rx, line);
            if (!err) {
                process_command(json_rx.as<JsonDocument>());
            }
        }
    }
}
