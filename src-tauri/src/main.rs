// Ventana nativa de granola-local.
//
// La ventana no hace nada más que mostrar `src/ui/web` y mantener vivo el sidecar de
// Python, que es donde vive toda la lógica. Se hablan por un WebSocket en 127.0.0.1.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;

use tauri::{Manager, RunEvent};

struct Sidecar(Mutex<Option<Child>>);

/// Raíz del repo. En desarrollo se corre desde `src-tauri/`; empaquetado, el sidecar
/// se resuelve contra el directorio de recursos del bundle.
fn project_root() -> PathBuf {
    if let Ok(dir) = std::env::var("GRANOLA_ROOT") {
        return PathBuf::from(dir);
    }
    let cwd = std::env::current_dir().unwrap_or_default();
    if cwd.join("src/ui/server.py").exists() {
        return cwd;
    }
    if let Some(parent) = cwd.parent() {
        if parent.join("src/ui/server.py").exists() {
            return parent.to_path_buf();
        }
    }
    cwd
}

fn spawn_sidecar(root: &PathBuf) -> std::io::Result<Child> {
    // El venv del proyecto, no el Python del sistema: los modelos y las dependencias
    // están ahí.
    let python = root.join(".venv/bin/python");
    let program = if python.exists() {
        python
    } else {
        PathBuf::from("python3")
    };

    Command::new(program)
        .args(["-u", "-m", "src.ui.server"])
        .current_dir(root)
        .env("PYTHONPATH", root)
        .spawn()
}

fn main() {
    let root = project_root();

    let app = tauri::Builder::default()
        .manage(Sidecar(Mutex::new(None)))
        .setup(move |app| {
            match spawn_sidecar(&root) {
                Ok(child) => {
                    let state = app.state::<Sidecar>();
                    *state.0.lock().unwrap() = Some(child);
                }
                Err(err) => {
                    eprintln!("granola-local: no se pudo arrancar el sidecar: {err}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("no se pudo construir la app");

    app.run(|handle, event| {
        // Sin esto queda un Python huérfano capturando audio después de cerrar la
        // ventana. Matarlo es parte de cerrar, no una optimización.
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Sidecar>().0.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
