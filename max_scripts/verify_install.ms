-- Version-dynamic startup verification script for Autodesk 3ds Max (2024-2028)
(
    fn jsonEscape str = (
        local s = str as string
        s = substituteString s "\\" "\\\\"
        s = substituteString s "\"" "\\\""
        s = substituteString s "\r" ""
        s = substituteString s "\n" "\\n"
        s
    )

    local mv = maxVersion()
    local majorRaw = mv[1]
    local maxYear = 1998 + (majorRaw / 1000)
    local tempDir = sysInfo.tempdir
    local verifyPath = tempDir + "max_verify.json"

    local userScriptsDir = getDir #userScripts
    local userMacrosDir = getDir #userMacros
    local currentRendererName = try ((renderers.current as string)) catch ("unknown")

    local rendererList = #()
    try (
        for cls in rendererClass.classes do (
            local clsName = cls as string
            append rendererList clsName
        )
    ) catch ()

    local f = createFile verifyPath
    if f != undefined do (
        format "{\n" to:f
        format "  \"status\": \"ok\",\n" to:f
        format "  \"max_version_raw\": %,\n" majorRaw to:f
        format "  \"year\": %,\n" maxYear to:f
        format "  \"version\": \"%\",\n" maxYear to:f
        format "  \"user_scripts\": \"%\",\n" (jsonEscape userScriptsDir) to:f
        format "  \"user_macros\": \"%\",\n" (jsonEscape userMacrosDir) to:f
        format "  \"current_renderer\": \"%\",\n" (jsonEscape currentRendererName) to:f
        format "  \"renderers\": [" to:f
        for i = 1 to rendererList.count do (
            if i > 1 do format ", " to:f
            format "\"%\"" (jsonEscape rendererList[i]) to:f
        )
        format "],\n" to:f
        format "  \"timestamp\": \"%\"\n" (jsonEscape localTime) to:f
        format "}\n" to:f
        close f
    )
    format "[MAX-AUTO] Verification report written to: %\n" verifyPath
)
