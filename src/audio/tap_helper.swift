// Helper de captura nativa para F4.
//
// Crea un Core Audio Process Tap global (macOS 14.4+) sobre el dispositivo de salida
// por defecto, lo mete en un dispositivo agregado privado, y escribe el audio por
// stdout: una línea JSON de cabecera y después float32 crudo, mono.
//
// Se compila solo desde src/audio/swift_tap.py. No necesita BlackHole ni Multi-Output.
//
//   swiftc -O src/audio/tap_helper.swift -o <bin>
//   <bin> [--exclude-pid N]...

import AudioToolbox
import CoreAudio
import Foundation

// MARK: - Utilidades de Core Audio

func check(_ status: OSStatus, _ what: String) {
    guard status == noErr else {
        let code = String(format: "%d", status)
        FileHandle.standardError.write("granola-tap: \(what) falló con OSStatus \(code)\n".data(using: .utf8)!)
        exit(2)
    }
}

func defaultOutputDevice() -> AudioObjectID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device),
          "leer el dispositivo de salida por defecto")
    return device
}

func deviceUID(_ device: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var uid: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    check(withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(device, &addr, 0, nil, &size, $0)
    }, "leer el UID del dispositivo")
    return uid as String
}

func tapUID(_ tap: AudioObjectID) -> String {
    // El UID que devuelve el tap creado es el que hay que poner en la lista del
    // agregado. Usar `desc.uuid` a ciegas da un agregado sin stream de entrada, que
    // se lee como silencio en vez de fallar.
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var uid: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    check(withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(tap, &addr, 0, nil, &size, $0)
    }, "leer el UID del tap")
    return uid as String
}

func inputChannels(_ device: AudioObjectID) -> Int {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioDevicePropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr, size > 0 else {
        return 0
    }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 8)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func tapFormat(_ tap: AudioObjectID) -> AudioStreamBasicDescription {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    check(AudioObjectGetPropertyData(tap, &addr, 0, nil, &size, &asbd), "leer el formato del tap")
    return asbd
}

// MARK: - Anillo entre el IOProc y el hilo que escribe
//
// El IOProc corre en un hilo de tiempo real: no puede escribir en un pipe, porque un
// pipe lleno lo bloquearía y cortaría el audio. Copia al anillo y sigue; el hilo
// principal es el que escribe. El candado se sostiene sólo lo que dura un memcpy de
// unos pocos cientos de muestras.

final class Ring {
    private var buffer: [Float]
    private var head = 0  // dónde escribe el IOProc
    private var tail = 0  // dónde lee el escritor
    private let lock = NSLock()
    private(set) var dropped = 0

    init(capacity: Int) { buffer = [Float](repeating: 0, count: capacity) }

    func write(_ src: UnsafePointer<Float>, _ count: Int) {
        lock.lock()
        defer { lock.unlock() }
        let free = buffer.count - (head - tail) - 1
        if count > free {
            dropped += count - free
            return
        }
        for i in 0..<count {
            buffer[(head + i) % buffer.count] = src[i]
        }
        head += count
    }

    func read(into dst: inout [Float]) -> Int {
        lock.lock()
        defer { lock.unlock() }
        let available = min(head - tail, dst.count)
        for i in 0..<available {
            dst[i] = buffer[(tail + i) % buffer.count]
        }
        tail += available
        return available
    }
}

// MARK: - Argumentos

var excluded: [AudioObjectID] = []
var args = Array(CommandLine.arguments.dropFirst())
while let i = args.firstIndex(of: "--exclude-pid") {
    guard i + 1 < args.count, let pid = Int32(args[i + 1]) else {
        FileHandle.standardError.write("granola-tap: --exclude-pid necesita un PID\n".data(using: .utf8)!)
        exit(64)
    }
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var object = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var value = pid
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr,
        UInt32(MemoryLayout<pid_t>.size), &value, &size, &object)
    if status == noErr, object != kAudioObjectUnknown { excluded.append(object) }
    args.removeSubrange(i...(i + 1))
}

// MARK: - Montaje del tap

let output = defaultOutputDevice()
let outputUID = deviceUID(output)

let env = ProcessInfo.processInfo.environment
let desc = env["GRANOLA_TAP_STEREO"] != nil
    ? CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
    : CATapDescription(monoGlobalTapButExcludeProcesses: excluded)
desc.name = "granola-local tap"
desc.isPrivate = env["GRANOLA_TAP_PUBLIC"] == nil
desc.muteBehavior = CATapMuteBehavior.unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
let tapStatus = AudioHardwareCreateProcessTap(desc, &tapID)
if tapStatus != noErr {
    FileHandle.standardError.write("""
    granola-tap: AudioHardwareCreateProcessTap falló con OSStatus \(tapStatus).
    Si es -4 (unimplemented) o 560947818 ('!pri'), falta el permiso de grabación de
    audio del sistema: Ajustes > Privacidad y seguridad > Grabación de audio.

    """.data(using: .utf8)!)
    exit(3)
}

let aggUID = "com.granola-local.tap.\(ProcessInfo.processInfo.processIdentifier)"
let aggDescription: [String: Any] = [
    kAudioAggregateDeviceNameKey: "granola-local Tap",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapUIDKey: tapUID(tapID),
        kAudioSubTapDriftCompensationKey: true,
    ]],
]

var aggID = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggDescription as CFDictionary, &aggID),
      "crear el dispositivo agregado")

let asbd = tapFormat(tapID)
let channels = Int(asbd.mChannelsPerFrame)
if ProcessInfo.processInfo.environment["GRANOLA_TAP_DEBUG"] != nil {
    FileHandle.standardError.write(
        "granola-tap: tapUID=\(tapUID(tapID)) descUUID=\(desc.uuid.uuidString) "
            .appending("aggInputCh=\(inputChannels(aggID)) salida=\(outputUID)\n")
            .data(using: .utf8)!)
    FileHandle.standardError.write("granola-tap: asbd sr=\(asbd.mSampleRate) ch=\(channels)"
        .appending(" fmt=\(asbd.mFormatFlags) bpf=\(asbd.mBytesPerFrame)\n").data(using: .utf8)!)
}
let sampleRate = asbd.mSampleRate

// Cabecera: el lado Python la lee antes de tocar un solo byte de audio.
let header = #"{"sample_rate": \#(sampleRate), "channels": 1, "format": "f32"}"# + "\n"
FileHandle.standardOutput.write(header.data(using: .utf8)!)

let ring = Ring(capacity: Int(sampleRate) * 8)  // 8 s de margen

let debug = ProcessInfo.processInfo.environment["GRANOLA_TAP_DEBUG"] != nil
nonisolated(unsafe) var debugCount = 0

var procID: AudioDeviceIOProcID?
var mixdown = [Float](repeating: 0, count: 8192)
check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inInput, _, _, _ in
    let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
    if debug, debugCount < 3 {
        debugCount += 1
        var line = "granola-tap: cb buffers=\(list.count)"
        for b in list { line += " [ch=\(b.mNumberChannels) bytes=\(b.mDataByteSize)]" }
        FileHandle.standardError.write((line + "\n").data(using: .utf8)!)
    }
    guard let first = list.first, first.mDataByteSize > 0, let data = first.mData else { return }

    if list.count > 1 {
        // No entrelazado: un buffer por canal. Se promedian a mono.
        let frames = Int(first.mDataByteSize) / MemoryLayout<Float>.size
        if frames > mixdown.count { return }
        for i in 0..<frames { mixdown[i] = 0 }
        for buf in list {
            guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for i in 0..<frames { mixdown[i] += p[i] }
        }
        let scale = 1.0 / Float(list.count)
        for i in 0..<frames { mixdown[i] *= scale }
        mixdown.withUnsafeBufferPointer { ring.write($0.baseAddress!, frames) }
        return
    }

    let samples = Int(first.mDataByteSize) / MemoryLayout<Float>.size
    let p = data.assumingMemoryBound(to: Float.self)
    if channels <= 1 {
        ring.write(p, samples)
        return
    }
    // Entrelazado con más de un canal: promedio por frame.
    let frames = samples / channels
    if frames > mixdown.count { return }
    for f in 0..<frames {
        var acc: Float = 0
        for c in 0..<channels { acc += p[f * channels + c] }
        mixdown[f] = acc / Float(channels)
    }
    mixdown.withUnsafeBufferPointer { ring.write($0.baseAddress!, frames) }
}, "crear el IOProc")

check(AudioDeviceStart(aggID, procID), "arrancar el dispositivo agregado")

// El hilo principal se queda en el bucle de escritura, así que no hay run loop que
// atienda un DispatchSourceSignal: el manejador tiene que ser el de C, sin capturas.
nonisolated(unsafe) var stopFlag: sig_atomic_t = 0
signal(SIGPIPE, SIG_IGN)
for sig in [SIGINT, SIGTERM] {
    signal(sig, { _ in stopFlag = 1 })
}

var chunk = [Float](repeating: 0, count: 4096)
let out = FileHandle.standardOutput
while stopFlag == 0 {
    let n = ring.read(into: &chunk)
    if n == 0 {
        usleep(5000)
        continue
    }
    let written = chunk.withUnsafeBytes { raw -> Int in
        write(out.fileDescriptor, raw.baseAddress!, n * MemoryLayout<Float>.size)
    }
    if written <= 0 { break }  // el lado Python cerró el pipe
}

AudioDeviceStop(aggID, procID)
if let procID { AudioDeviceDestroyIOProcID(aggID, procID) }
AudioHardwareDestroyAggregateDevice(aggID)
AudioHardwareDestroyProcessTap(tapID)
if ring.dropped > 0 {
    FileHandle.standardError.write("granola-tap: \(ring.dropped) muestras perdidas\n".data(using: .utf8)!)
}
