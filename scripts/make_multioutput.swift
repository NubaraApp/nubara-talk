// Crea un Multi-Output Device (agregado "apilado") con la salida real + BlackHole, y lo
// deja como salida por defecto del sistema.
//
// Esto reemplaza los pasos de GUI en Audio MIDI Setup. Es el mismo objeto de Core Audio
// que crea esa app: un AudioAggregateDevice con kAudioAggregateDeviceIsStackedKey.
//
//   swiftc -O scripts/make_multioutput.swift -o /tmp/make_multioutput
//   /tmp/make_multioutput [substring-salida-real] [substring-loopback]

import CoreAudio
import Foundation

let AGGREGATE_UID = "com.granola-local.multioutput"
let AGGREGATE_NAME = "granola-local Multi-Output"

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func allDevices() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(systemObject, &addr, 0, nil, &size) == noErr else {
        return []
    }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &ids) == noErr else {
        return []
    }
    return ids
}

func stringProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: CFString?
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    guard status == noErr, let value else { return nil }
    return value as String
}

func outputChannels(_ id: AudioDeviceID) -> Int {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else {
        return 0
    }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = raw.assumingMemoryBound(to: AudioBufferList.self)
    return UnsafeMutableAudioBufferListPointer(list).reduce(0) { $0 + Int($1.mNumberChannels) }
}

func findOutput(_ needle: String) -> (id: AudioDeviceID, uid: String, name: String)? {
    let target = needle.lowercased()
    for id in allDevices() where outputChannels(id) > 0 {
        guard let name = stringProperty(id, kAudioObjectPropertyName),
            let uid = stringProperty(id, kAudioDevicePropertyDeviceUID)
        else { continue }
        if name.lowercased().contains(target) { return (id, uid, name) }
    }
    return nil
}

func destroyExistingAggregate() {
    for id in allDevices() {
        if stringProperty(id, kAudioDevicePropertyDeviceUID) == AGGREGATE_UID {
            AudioHardwareDestroyAggregateDevice(id)
        }
    }
}

func setDefaultOutput(_ id: AudioDeviceID) -> OSStatus {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value = id
    return AudioObjectSetPropertyData(
        systemObject, &addr, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &value)
}

func die(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("ERROR: " + message + "\n").utf8))
    exit(1)
}

let args = CommandLine.arguments
let realNeedle = args.count > 1 ? args[1] : "Bocinas"
let loopNeedle = args.count > 2 ? args[2] : "BlackHole"

guard let real = findOutput(realNeedle) else {
    die("no encontré una salida que contenga \(realNeedle.debugDescription)")
}
guard let loop = findOutput(loopNeedle) else {
    die("no encontré una salida que contenga \(loopNeedle.debugDescription)")
}

destroyExistingAggregate()

// El primer sub-dispositivo es el maestro de reloj: va la salida real. La corrección de
// deriva se activa solo en el esclavo; activarla en el maestro no tiene sentido y
// degrada el audio.
let description: [String: Any] = [
    kAudioAggregateDeviceNameKey: AGGREGATE_NAME,
    kAudioAggregateDeviceUIDKey: AGGREGATE_UID,
    kAudioAggregateDeviceMainSubDeviceKey: real.uid,
    kAudioAggregateDeviceIsStackedKey: 1,
    kAudioAggregateDeviceIsPrivateKey: 0,
    kAudioAggregateDeviceSubDeviceListKey: [
        [kAudioSubDeviceUIDKey: real.uid, kAudioSubDeviceDriftCompensationKey: 0],
        [kAudioSubDeviceUIDKey: loop.uid, kAudioSubDeviceDriftCompensationKey: 1],
    ],
]

var aggregateID = AudioDeviceID(0)
let created = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID)
guard created == noErr, aggregateID != 0 else {
    die("AudioHardwareCreateAggregateDevice falló con estado \(created)")
}

print("creado: \(AGGREGATE_NAME) (id \(aggregateID))")
print("  maestro de reloj: \(real.name)")
print("  loopback:         \(loop.name)  [corrección de deriva activada]")

let assigned = setDefaultOutput(aggregateID)
if assigned == noErr {
    print("salida por defecto del sistema: \(AGGREGATE_NAME)")
} else {
    die("no pude ponerlo como salida por defecto, estado \(assigned)")
}
