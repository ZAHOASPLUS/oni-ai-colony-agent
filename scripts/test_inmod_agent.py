"""Source-contract checks plus an optional compiled C# core harness.

Source checks are not execution evidence for C#. The executable suite is
explicitly skipped when its .NET SDK / Newtonsoft.Json dependency is absent.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class InModSourceContractTests(unittest.TestCase):
    def test_single_schema_is_embedded_and_names_match(self):
        schema = json.loads((ROOT / "tools/oni-tools.json").read_text())
        names = [tool["name"] for tool in schema["tools"]]
        self.assertEqual(len(names), len(set(names)))
        registry = (ROOT / "mod/Source/Api/ApiTools.cs").read_text()
        handlers = set(re.findall(r'\{\s*"([a-z_]+)",\s*new Tool\(', registry))
        self.assertEqual(set(names), handlers | {"wait"})
        project = ET.parse(ROOT / "mod/OniAiProbeMod.csproj")
        resources = project.findall(".//EmbeddedResource")
        self.assertTrue(any(x.attrib.get("LogicalName") == "OniAiProbeMod.oni-tools.json"
                            and x.attrib["Include"].replace("\\", "/") == "../tools/oni-tools.json"
                            for x in resources))

    def test_validator_covers_present_schema_vocabulary(self):
        supported = {"type", "properties", "additionalProperties", "required", "enum",
                     "items", "minimum", "maximum", "minItems", "maxItems", "description"}
        def inspect(node):
            self.assertLessEqual(set(node), supported)
            for child in node.get("properties", {}).values():
                inspect(child)
            if "items" in node:
                inspect(node["items"])
        schema = json.loads((ROOT / "tools/oni-tools.json").read_text())
        for tool in schema["tools"]:
            inspect(tool["parameters"])

    def test_guard_still_enabled_and_no_agent_path_load(self):
        mod = (ROOT / "mod/Source/Mod.cs").read_text()
        self.assertIn('AutosaveGuard.Activate("api_mode")', mod)
        dispatcher = (ROOT / "mod/Source/Agent/GameToolDispatcher.cs").read_text()
        self.assertIn('toolName == "load_save"', dispatcher)
        self.assertIn('"rollback_unavailable"', dispatcher)
        self.assertNotIn("ApiLifecycle.Load(", dispatcher)
        self.assertIn('["limit"] = Math.Min(100, 512 - events.Count)', dispatcher)
        lifecycle = (ROOT / "mod/Source/Patches/LifecyclePatches.cs").read_text()
        self.assertEqual(lifecycle.count("ApiEvents.ResetSession();"), 2)
        self.assertEqual(lifecycle.count("ApiPrintables.ResetSession();"), 2)

    def test_config_has_no_key_and_network_does_not_follow_redirects(self):
        config = (ROOT / "mod/Source/Agent/AgentConfig.cs").read_text()
        serialized = re.findall(r'\[JsonProperty\]\s+public\s+\w+\s+(\w+)', config)
        self.assertFalse(any("key" in x.lower() or "secret" in x.lower() for x in serialized))
        client = (ROOT / "mod/Source/Agent/OpenAiCompatibleClient.cs").read_text()
        self.assertIn("AllowAutoRedirect = false", client)

    def test_main_thread_pump_does_not_block(self):
        for name in ("GameToolDispatcher.cs", "AgentHost.cs"):
            source = (ROOT / "mod/Source/Agent" / name).read_text()
            self.assertNotIn("Thread.Sleep(", source)
            self.assertNotIn(".Wait(", source)
        source = (ROOT / "mod/Source/Agent/GameToolDispatcher.cs").read_text()
        self.assertIn("canDispatch(pending.Session, pending.Generation)", source)
        self.assertIn('"agent", pending.Session', source)


class CompiledAgentTests(unittest.TestCase):
    def test_real_csharp_core_harness(self):
        if not shutil.which("dotnet"):
            self.skipTest(".NET SDK not installed; C# implementation has NOT been executed")
        managed = Path(os.environ.get("ONI_MANAGED_DIR", ""))
        json_dll = Path(os.environ.get("NEWTONSOFT_JSON_PATH", str(managed / "Newtonsoft.Json.dll")))
        if not json_dll.is_file():
            self.skipTest("Set NEWTONSOFT_JSON_PATH; no package installation is performed")
        result = subprocess.run(["dotnet", "run", "--project", str(ROOT / "tools/AgentRuntimeHarness"),
                                 "--configuration", "Release", "-p:NewtonsoftJsonPath=" + str(json_dll)],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-6000:])


if __name__ == "__main__":
    unittest.main()
