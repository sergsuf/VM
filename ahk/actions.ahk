#Requires AutoHotkey v2.0
#SingleInstance Force

; Match Python UIA Per-Monitor v2 DPI awareness and screen coordinates
try DllCall("SetProcessDpiAwarenessContext", "ptr", -4, "ptr")
CoordMode "Mouse", "Screen"
SetMouseDelay 30
SetKeyDelay 30

if A_Args.Length < 1 {
    ExitApp 2
}

action := StrLower(A_Args[1])

ActivateIfProvided(hwndStr) {
    if hwndStr != "" && hwndStr != "0" {
        winSpec := "ahk_id " hwndStr
        if WinExist(winSpec) {
            WinActivate winSpec
            WinWaitActive winSpec, , 2
            Sleep 100
        }
    }
}

try {
    switch action {
        case "click":
            if A_Args.Length < 3 {
                ExitApp 2
            }
            x := Integer(A_Args[2])
            y := Integer(A_Args[3])
            hwnd := A_Args.Length >= 4 ? A_Args[4] : ""
            ActivateIfProvided(hwnd)
            MouseMove x, y, 2
            Sleep 50
            Click x, y
            ExitApp 0

        case "ctrlclick":
            ; ctrlclick <hwnd> <controlNameOrClassNN>  OR  ctrlclick <hwnd> <clientX> <clientY>
            if A_Args.Length < 3 {
                ExitApp 2
            }
            winSpec := "ahk_id " A_Args[2]
            if !WinExist(winSpec) {
                ExitApp 4
            }
            if A_Args.Length >= 4 {
                cx := Integer(A_Args[3])
                cy := Integer(A_Args[4])
                ControlClick "X" cx " Y" cy, winSpec, , "Left", 1, "NA"
            } else {
                ControlClick A_Args[3], winSpec, , "Left", 1, "NA"
            }
            ExitApp 0

        case "send":
            if A_Args.Length < 2 {
                ExitApp 2
            }
            keys := A_Args[2]
            hwnd := A_Args.Length >= 3 ? A_Args[3] : ""
            ActivateIfProvided(hwnd)
            Send keys
            ExitApp 0

        case "text":
            if A_Args.Length < 2 {
                ExitApp 2
            }
            txt := A_Args[2]
            hwnd := A_Args.Length >= 3 ? A_Args[3] : ""
            ActivateIfProvided(hwnd)
            SendText txt
            ExitApp 0

        case "activate":
            if A_Args.Length < 2 {
                ExitApp 2
            }
            ActivateIfProvided(A_Args[2])
            ExitApp 0

        case "close":
            if A_Args.Length < 2 {
                ExitApp 2
            }
            winSpec := "ahk_id " A_Args[2]
            if WinExist(winSpec) {
                WinClose winSpec
            }
            ExitApp 0

        default:
            ExitApp 3
    }
} catch as err {
    FileAppend "AHK Error: " err.Message "`n", "**"
    ExitApp 1
}
