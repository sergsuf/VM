-- Simple startup verification script for 3ds Max 2025 VM automation
(
    local tempDir = sysInfo.tempdir
    local markerPath = tempDir + "max_2025_ready.txt"
    local f = createFile markerPath
    if f != undefined do (
        format "3dsMaxVersion: %\n" (maxVersion())[1] to:f
        format "UserScripts: %\n" (getDir #userScripts) to:f
        format "Timestamp: %\n" localTime to:f
        close f
    )
    format "[VM-AUTO] Startup verification marker written to: %\n" markerPath
)
