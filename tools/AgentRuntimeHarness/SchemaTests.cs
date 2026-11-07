using System;
using System.Linq;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Api;
using OniAiProbeMod.Agent;

internal static class SchemaTests
{
    internal static void Run()
    {
        Assert(ApiToolSchema.Names.Count() == ApiToolSchema.OpenAiTools().Count, "dynamic schema count");
        ApiToolSchema.CheckHandlers(ApiToolSchema.Names.Where(n => n != "wait"));
        ApiToolSchema.Validate("get_status", new JObject());
        ApiToolSchema.Validate("dig", new JObject { ["x"] = 1, ["y"] = 2 });
        Refused("get_status", new JObject { ["wolrd"] = 0 });
        Refused("set_priority", new JObject { ["id"] = 7 });
        Refused("get_status", new JObject { ["world"] = JValue.CreateNull() });
        Refused("configure", new JObject { ["id"] = 2, ["crew"] = "wrong", ["enabled"] = true });
        Refused("save_checkpoint", new JObject { ["note"] = 42 });
        Refused("set_speed", new JObject { ["speed"] = 1.5 });
        Refused("get_map", new JObject { ["layers"] = new JArray("not_a_layer") });
        Refused("missing_tool", new JObject());
        Assert(AgentWriteLease.Allows("external"), "external initially owns writes");
        Assert(AgentWriteLease.Acquire("a"), "lease acquisition");
        Assert(!AgentWriteLease.Allows("external"), "external blocked");
        Assert(AgentWriteLease.Allows("agent", "a"), "owner allowed");
        Assert(!AgentWriteLease.Allows("agent", "b"), "other session blocked");
        Assert(!AgentWriteLease.Acquire("b"), "other session cannot steal");
        AgentWriteLease.Release("b");
        Assert(!AgentWriteLease.Allows("external"), "wrong release is inert");
        AgentWriteLease.Release("a");
        Assert(AgentWriteLease.Allows("external"), "released lease");
    }

    private static void Refused(string name, JObject arguments)
    {
        try { ApiToolSchema.Validate(name, arguments); }
        catch (ApiException) { return; }
        throw new Exception("Expected schema refusal: " + name);
    }
    private static void Assert(bool condition, string scenario)
    {
        if (!condition) throw new Exception("Schema/lease test failed: " + scenario);
    }
}
