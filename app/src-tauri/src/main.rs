// onec-agent в трее: запускает сервер ИИ-консультанта (Python), показывает состояние встроенной
// модели и открывает панель управления. Закрытие окна прячет его в трей; «Выход» останавливает
// сервер, а вместе с ним и модель (сервер гасит llama-server при остановке).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const BASE: &str = "http://127.0.0.1:8765";

/// Сервер, который запустило приложение (если он уже работал — не наш, при выходе не трогаем).
struct Server(Mutex<Option<Child>>);

/// Каталог сервера: ONEC_AGENT_SERVER_DIR, иначе server/ рядом с app/ в репозитории.
fn server_dir() -> PathBuf {
    std::env::var("ONEC_AGENT_SERVER_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../server"))
}

fn token() -> String {
    std::fs::read_to_string(server_dir().join("data/token"))
        .map(|t| t.trim().to_string())
        .unwrap_or_default()
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new().timeout(Duration::from_secs(5)).build()
}

fn server_alive() -> bool {
    agent().get(&format!("{BASE}/health")).call().is_ok()
}

fn start_server() -> Option<Child> {
    if server_alive() {
        return None;
    }
    let dir = server_dir();
    let log = std::fs::File::create(dir.join("logs/app-server.log")).ok();
    let mut cmd = Command::new(dir.join("run.sh"));
    cmd.current_dir(&dir).stdin(Stdio::null());
    if let Some(log) = log {
        cmd.stdout(log.try_clone().unwrap()).stderr(log);
    }
    let child = cmd.spawn().ok()?;
    for _ in 0..60 {
        if server_alive() {
            break;
        }
        thread::sleep(Duration::from_millis(500));
    }
    Some(child)
}

fn stop_server(app: &AppHandle) {
    let state = app.state::<Server>();
    let mut guard = state.0.lock().unwrap();
    if let Some(mut child) = guard.take() {
        // SIGTERM, а не kill: сервер должен успеть остановить llama-server и закрыть соединения.
        let _ = Command::new("kill").arg("-TERM").arg(child.id().to_string()).status();
        for _ in 0..40 {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(250));
        }
        let _ = child.kill();
    }
}

fn model_action(action: &str) {
    let _ = agent()
        .post(&format!("{BASE}/panel/api/model/{action}"))
        .set("X-Agent-Token", &token())
        .send_json(serde_json::json!({}));
}

/// Строка состояния для меню трея и признак «модель работает».
fn status_line() -> (String, bool) {
    let resp = agent()
        .get(&format!("{BASE}/panel/api/status"))
        .set("X-Agent-Token", &token())
        .call();
    let Ok(resp) = resp else {
        return ("Сервер не отвечает".into(), false);
    };
    let Ok(s) = resp.into_json::<serde_json::Value>() else {
        return ("Сервер не отвечает".into(), false);
    };
    let state = s["state"].as_str().unwrap_or("");
    let preset = s["preset"].as_str().unwrap_or("");
    let mem = s["memory_mb"].as_f64().unwrap_or(0.0) / 1024.0;
    match state {
        "ready" => (format!("● Модель работает: {preset}, {mem:.1} ГБ"), true),
        "starting" => (format!("◐ Модель загружается: {preset}…"), true),
        "error" => ("✕ Ошибка модели — откройте панель".into(), false),
        _ => ("○ Модель остановлена".into(), false),
    }
}

fn open_panel(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("panel") {
        let _ = window.show();
        let _ = window.set_focus();
        return;
    }
    // Токен — скриптом до загрузки страницы: в адресной строке его быть не должно.
    let script = format!("window.__AGENT_TOKEN__ = {};", serde_json::to_string(&token()).unwrap());
    let url = WebviewUrl::External(format!("{BASE}/panel").parse().unwrap());
    let _ = WebviewWindowBuilder::new(app, "panel", url)
        .title("onec-agent")
        .inner_size(780.0, 860.0)
        .initialization_script(&script)
        .build();
}

fn main() {
    tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory); // только трей, без значка в Dock

            let child = start_server();
            *app.state::<Server>().0.lock().unwrap() = child;

            let status = MenuItem::with_id(app, "status", "…", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Открыть панель", true, None::<&str>)?;
            let start = MenuItem::with_id(app, "start", "Запустить модель", true, None::<&str>)?;
            let stop = MenuItem::with_id(app, "stop", "Остановить модель", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&status, &sep, &open, &start, &stop, &sep2, &quit])?;

            let tray = TrayIconBuilder::with_id("main")
                .icon(Image::from_bytes(include_bytes!("../icons/tray.png"))?)
                .icon_as_template(true)
                .tooltip("onec-agent")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => open_panel(app),
                    "start" => {
                        thread::spawn(|| model_action("start"));
                    }
                    "stop" => {
                        thread::spawn(|| model_action("stop"));
                    }
                    "quit" => {
                        stop_server(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // Состояние модели в меню и подсказке трея — опрос раз в 3 секунды.
            thread::spawn(move || loop {
                let (line, running) = status_line();
                let _ = status.set_text(&line);
                let _ = start.set_enabled(!running);
                let _ = stop.set_enabled(running);
                let _ = tray.set_tooltip(Some(format!("onec-agent — {line}")));
                thread::sleep(Duration::from_secs(3));
            });

            open_panel(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close(); // окно прячется, приложение и сервер продолжают работать
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("не удалось запустить onec-agent")
        .run(|app, event| {
            if let RunEvent::ExitRequested { api, code, .. } = event {
                if code.is_none() {
                    api.prevent_exit(); // закрыли последнее окно — живём в трее
                } else {
                    stop_server(app);
                }
            }
        });
}
