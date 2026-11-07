using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>The embedded copy is linked directly from tools/oni-tools.json.</summary>
    internal static class ApiToolSchema
    {
        private static readonly Dictionary<string, JObject> Definitions = Load();

        internal static IEnumerable<string> Names => Definitions.Keys;

        private static Dictionary<string, JObject> Load()
        {
            using (Stream stream = typeof(ApiToolSchema).Assembly.GetManifestResourceStream("OniAiProbeMod.oni-tools.json"))
            {
                if (stream == null) throw new InvalidOperationException("Embedded tool schema is missing.");
                using (StreamReader reader = new StreamReader(stream))
                {
                    JObject root = JObject.Parse(reader.ReadToEnd());
                    return ((JArray)root["tools"]).Cast<JObject>().ToDictionary(t => (string)t["name"], StringComparer.Ordinal);
                }
            }
        }

        internal static JArray OpenAiTools()
        {
            return new JArray(Definitions.Values.Select(t => new JObject
            {
                ["type"] = "function",
                ["function"] = t.DeepClone()
            }));
        }

        internal static void CheckHandlers(IEnumerable<string> handlers)
        {
            // wait is an asynchronous adapter, never a blocking main-thread handler.
            HashSet<string> actual = new HashSet<string>(handlers, StringComparer.Ordinal) { "wait" };
            if (!actual.SetEquals(Definitions.Keys))
                throw new InvalidOperationException("Tool schema/handler mismatch: " +
                    string.Join(",", actual.Except(Definitions.Keys).Concat(Definitions.Keys.Except(actual))));
        }

        internal static JObject Validate(string name, JObject arguments)
        {
            JObject definition;
            if (!Definitions.TryGetValue(name ?? string.Empty, out definition))
                throw new ApiException("unknown_tool", "Unknown tool: " + name);
            if (arguments == null) throw new ApiException("invalid_argument", "Arguments must be an object.");
            ValidateValue(arguments, (JObject)definition["parameters"], name);
            return arguments;
        }

        private static void ValidateValue(JToken value, JObject schema, string path)
        {
            JToken type = schema["type"];
            string[] allowed = type is JArray types ? types.Values<string>().ToArray() : new[] { (string)type };
            if (!allowed.Any(t => MatchesType(value, t)))
                throw new ApiException("invalid_argument", path + " has the wrong JSON type.");
            JArray choices = schema["enum"] as JArray;
            if (choices != null && !choices.Any(c => JToken.DeepEquals(c, value)))
                throw new ApiException("invalid_argument", path + " is not an allowed value.");
            if (value.Type == JTokenType.Integer || value.Type == JTokenType.Float)
            {
                double number = value.Value<double>();
                if (double.IsNaN(number) || double.IsInfinity(number) ||
                    (schema["minimum"] != null && number < (double)schema["minimum"]) ||
                    (schema["maximum"] != null && number > (double)schema["maximum"]))
                    throw new ApiException("invalid_argument", path + " is out of range.");
            }
            if (value is JObject obj)
            {
                JObject properties = schema["properties"] as JObject ?? new JObject();
                foreach (string required in (schema["required"] as JArray ?? new JArray()).Values<string>())
                    if (obj.Property(required) == null)
                        throw new ApiException("invalid_argument", path + " requires " + required + ".");
                foreach (JProperty property in obj.Properties())
                {
                    JObject child = properties[property.Name] as JObject;
                    if (child != null) ValidateValue(property.Value, child, path + "." + property.Name);
                    else if ((bool?)schema["additionalProperties"] == false)
                        throw new ApiException("invalid_argument", path + " has no argument " + property.Name + ".");
                }
            }
            if (value is JArray array)
            {
                if ((schema["minItems"] != null && array.Count < (int)schema["minItems"]) ||
                    (schema["maxItems"] != null && array.Count > (int)schema["maxItems"]))
                    throw new ApiException("invalid_argument", path + " has an invalid item count.");
                JObject item = schema["items"] as JObject;
                if (item != null) for (int i = 0; i < array.Count; i++) ValidateValue(array[i], item, path + "[" + i + "]");
            }
        }

        private static bool MatchesType(JToken value, string type)
        {
            switch (type)
            {
                case "object": return value.Type == JTokenType.Object;
                case "array": return value.Type == JTokenType.Array;
                case "string": return value.Type == JTokenType.String;
                case "integer": return value.Type == JTokenType.Integer;
                case "number": return value.Type == JTokenType.Integer || value.Type == JTokenType.Float;
                case "boolean": return value.Type == JTokenType.Boolean;
                case "null": return value.Type == JTokenType.Null;
                case null: return true;
                default: throw new InvalidOperationException("Unsupported schema type: " + type);
            }
        }
    }
}
