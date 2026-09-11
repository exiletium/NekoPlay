-- video2x.lua
--
-- Copyright 2026 Diego Povliuk
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU General Public License as published by
-- the Free Software Foundation, either version 3 of the License, or
-- (at your option) any later version.
--
-- This program is distributed in the hope that it will be useful,
-- but WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
-- GNU General Public License for more details.
--
-- You should have received a copy of the GNU General Public License
-- along with this program.  If not, see <https://www.gnu.org/licenses/>.
--
-- SPDX-License-Identifier: GPL-3.0-or-later

-- Lets the app substitute a rendered file for the one mpv is about to open.
--
-- This is the same mechanism ytdl_hook uses: an on_load hook that changes
-- stream-open-filename. Everything mpv reports about the file - path,
-- filename, media-title, the playlist entry, the watch history - keeps
-- referring to the original, and only the bytes come from somewhere else.
--
-- The decision lives in Python (video2x.py), which knows the settings and
-- runs the render. The hook is deferred while it decides, which can take a
-- while: for a pre-render, the whole render. mpv keeps answering property
-- reads and commands in the meantime, so the UI stays responsive.

local pending = {}
local next_id = 0

-- The app's answer: a path to open instead, or "" to open the original.
mp.register_script_message("video2x-open", function(id, target)
    local hook = pending[id]
    if hook == nil then
        return
    end
    pending[id] = nil
    if target ~= nil and target ~= "" then
        mp.set_property("stream-open-filename", target)
    end
    hook:cont()
end)

mp.add_hook("on_load", 50, function(hook)
    -- Set by the app, so that with the feature off this costs one property
    -- read and no round trip. The native read matters: the string form of
    -- a user-data node is its JSON, quotes included, and never equals off.
    if mp.get_property_native("user-data/video2x/mode", "off") == "off" then
        return
    end

    next_id = next_id + 1
    local id = tostring(next_id)
    pending[id] = hook
    hook:defer()
    mp.commandv("script-message", "video2x-want", id,
                mp.get_property("stream-open-filename"))
end)
