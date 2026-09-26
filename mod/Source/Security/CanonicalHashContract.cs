using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Security
{
    /// <summary>
    /// Serializer-independent hashing for approval security boundaries.
    /// The byte grammar is documented in docs/canonical-hash-contract-v1.md.
    /// </summary>
    internal static class CanonicalHashContract
    {
        internal const string ContractId = "oni.typed-canonical-sha256.v1";

        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private static readonly byte[] Domain = BuildDomain();

        internal static string Hash(JToken value)
        {
            byte[] encoded = Encode(value);
            using (SHA256 sha256 = SHA256.Create())
            {
                byte[] digest = sha256.ComputeHash(encoded);
                StringBuilder output = new StringBuilder(7 + digest.Length * 2);
                output.Append("sha256:");
                foreach (byte item in digest)
                {
                    output.Append(item.ToString("x2", CultureInfo.InvariantCulture));
                }
                return output.ToString();
            }
        }

        internal static byte[] Encode(JToken value)
        {
            using (MemoryStream output = new MemoryStream())
            {
                output.Write(Domain, 0, Domain.Length);
                AppendValue(output, value, "$");
                return output.ToArray();
            }
        }

        /// <summary>
        /// Json.NET 7 replaces duplicate object names during JObject.Load. Scan
        /// once first so escaped aliases (for example a and \u0061) fail closed.
        /// </summary>
        internal static JObject ParseObjectStrict(string json)
        {
            ValidateStrictJson(json);
            using (StringReader stringReader = new StringReader(json))
            using (JsonTextReader jsonReader = CreateReader(stringReader))
            {
                JObject value = JObject.Load(jsonReader);
                if (jsonReader.Read())
                {
                    throw new CanonicalHashException("JSON contains trailing content.");
                }
                return value;
            }
        }

        private static JsonTextReader CreateReader(TextReader source)
        {
            JsonTextReader reader = new JsonTextReader(source);
            reader.DateParseHandling = DateParseHandling.None;
            reader.FloatParseHandling = FloatParseHandling.Double;
            return reader;
        }

        private static void ValidateStrictJson(string json)
        {
            ValidateJsonStrings(json);
            Stack<JsonScope> scopes = new Stack<JsonScope>();
            using (StringReader stringReader = new StringReader(json))
            using (JsonTextReader reader = CreateReader(stringReader))
            {
                while (reader.Read())
                {
                    switch (reader.TokenType)
                    {
                        case JsonToken.StartObject:
                            scopes.Push(JsonScope.Object());
                            break;
                        case JsonToken.StartArray:
                            scopes.Push(JsonScope.Array());
                            break;
                        case JsonToken.EndObject:
                            RequireScope(scopes, true);
                            scopes.Pop();
                            break;
                        case JsonToken.EndArray:
                            RequireScope(scopes, false);
                            scopes.Pop();
                            break;
                        case JsonToken.PropertyName:
                        {
                            RequireScope(scopes, true);
                            string name = Convert.ToString(reader.Value, CultureInfo.InvariantCulture);
                            ValidateUnicode(name, "object key");
                            if (!scopes.Peek().Names.Add(name))
                            {
                                throw new CanonicalHashException(
                                    "JSON contains duplicate decoded object key " + name + ".");
                            }
                            break;
                        }
                        case JsonToken.String:
                            ValidateUnicode(
                                Convert.ToString(reader.Value, CultureInfo.InvariantCulture),
                                "string");
                            break;
                        case JsonToken.Float:
                        {
                            double number = Convert.ToDouble(reader.Value, CultureInfo.InvariantCulture);
                            if (double.IsNaN(number) || double.IsInfinity(number))
                            {
                                throw new CanonicalHashException("JSON contains a non-finite float.");
                            }
                            break;
                        }
                        case JsonToken.Comment:
                        case JsonToken.Undefined:
                        case JsonToken.Date:
                        case JsonToken.Bytes:
                        case JsonToken.Raw:
                            throw new CanonicalHashException(
                                "JSON contains unsupported token " + reader.TokenType + ".");
                    }
                }
            }

            if (scopes.Count != 0)
            {
                throw new CanonicalHashException("JSON ended inside a container.");
            }
        }

        private static void ValidateJsonStrings(string json)
        {
            bool insideString = false;
            for (int index = 0; index < json.Length; index++)
            {
                char current = json[index];
                if (!insideString)
                {
                    if (current == '"')
                    {
                        insideString = true;
                    }
                    continue;
                }

                if (current == '"')
                {
                    insideString = false;
                    continue;
                }
                if (current < 0x20)
                {
                    throw new CanonicalHashException("JSON string contains an unescaped control character.");
                }
                if (current == '\\')
                {
                    if (++index >= json.Length)
                    {
                        throw new CanonicalHashException("JSON string ends inside an escape.");
                    }
                    char escape = json[index];
                    if (escape == 'u')
                    {
                        int codeUnit = ReadEscapedCodeUnit(json, ref index);
                        if (codeUnit >= 0xd800 && codeUnit <= 0xdbff)
                        {
                            if (index + 2 >= json.Length || json[index + 1] != '\\' || json[index + 2] != 'u')
                            {
                                throw new CanonicalHashException("JSON string contains an unpaired high surrogate.");
                            }
                            index += 2;
                            int low = ReadEscapedCodeUnit(json, ref index);
                            if (low < 0xdc00 || low > 0xdfff)
                            {
                                throw new CanonicalHashException("JSON string contains an unpaired high surrogate.");
                            }
                        }
                        else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff)
                        {
                            throw new CanonicalHashException("JSON string contains an unpaired low surrogate.");
                        }
                    }
                    else if (escape != '"' && escape != '\\' && escape != '/' &&
                        escape != 'b' && escape != 'f' && escape != 'n' &&
                        escape != 'r' && escape != 't')
                    {
                        throw new CanonicalHashException("JSON string contains an invalid escape.");
                    }
                    continue;
                }
                if (char.IsHighSurrogate(current))
                {
                    if (index + 1 >= json.Length || !char.IsLowSurrogate(json[index + 1]))
                    {
                        throw new CanonicalHashException("JSON string contains an unpaired high surrogate.");
                    }
                    index++;
                }
                else if (char.IsLowSurrogate(current))
                {
                    throw new CanonicalHashException("JSON string contains an unpaired low surrogate.");
                }
            }

            if (insideString)
            {
                throw new CanonicalHashException("JSON ends inside a string.");
            }
        }

        private static int ReadEscapedCodeUnit(string json, ref int index)
        {
            if (index + 4 >= json.Length)
            {
                throw new CanonicalHashException("JSON contains a truncated Unicode escape.");
            }
            int value = 0;
            for (int offset = 1; offset <= 4; offset++)
            {
                int digit = HexValue(json[index + offset]);
                if (digit < 0)
                {
                    throw new CanonicalHashException("JSON contains an invalid Unicode escape.");
                }
                value = (value << 4) | digit;
            }
            index += 4;
            return value;
        }

        private static int HexValue(char value)
        {
            if (value >= '0' && value <= '9')
            {
                return value - '0';
            }
            if (value >= 'a' && value <= 'f')
            {
                return value - 'a' + 10;
            }
            if (value >= 'A' && value <= 'F')
            {
                return value - 'A' + 10;
            }
            return -1;
        }

        private static void RequireScope(Stack<JsonScope> scopes, bool objectScope)
        {
            if (scopes.Count == 0 || scopes.Peek().IsObject != objectScope)
            {
                throw new CanonicalHashException("JSON container nesting is invalid.");
            }
        }

        private static void AppendValue(Stream output, JToken value, string path)
        {
            if (value == null || value.Type == JTokenType.Null)
            {
                WriteAscii(output, "n");
                return;
            }

            switch (value.Type)
            {
                case JTokenType.Boolean:
                    WriteAscii(output, value.Value<bool>() ? "b1" : "b0");
                    return;
                case JTokenType.Integer:
                    AppendInteger(output, value, path);
                    return;
                case JTokenType.Float:
                    AppendFloat(output, value, path);
                    return;
                case JTokenType.String:
                    AppendString(output, value.Value<string>(), path);
                    return;
                case JTokenType.Array:
                    AppendArray(output, (JArray)value, path);
                    return;
                case JTokenType.Object:
                    AppendObject(output, (JObject)value, path);
                    return;
                default:
                    throw new CanonicalHashException(
                        path + " has unsupported canonical token " + value.Type + ".");
            }
        }

        private static void AppendInteger(Stream output, JToken value, string path)
        {
            string text = value.ToString(Formatting.None);
            BigInteger integer;
            if (!BigInteger.TryParse(
                    text,
                    NumberStyles.Integer,
                    CultureInfo.InvariantCulture,
                    out integer))
            {
                throw new CanonicalHashException(path + " is not a canonical integer.");
            }
            if (integer < long.MinValue || integer > long.MaxValue)
            {
                throw new CanonicalHashException(path + " integer is outside signed 64-bit range.");
            }
            WriteAscii(output, "i");
            WriteAscii(output, integer.ToString(CultureInfo.InvariantCulture));
            WriteAscii(output, ";");
        }

        private static void AppendFloat(Stream output, JToken value, string path)
        {
            double number;
            try
            {
                number = value.Value<double>();
            }
            catch (Exception error)
            {
                throw new CanonicalHashException(path + " cannot be represented as binary64.", error);
            }
            if (double.IsNaN(number) || double.IsInfinity(number))
            {
                throw new CanonicalHashException(path + " contains a non-finite float.");
            }

            ulong bits = unchecked((ulong)BitConverter.DoubleToInt64Bits(number));
            WriteAscii(output, "f");
            WriteAscii(output, bits.ToString("x16", CultureInfo.InvariantCulture));
            WriteAscii(output, ";");
        }

        private static void AppendString(Stream output, string value, string path)
        {
            byte[] encoded = EncodeUtf8(value, path);
            AppendEncodedString(output, encoded);
        }

        private static void AppendEncodedString(Stream output, byte[] encoded)
        {
            WriteAscii(output, "s");
            WriteAscii(output, encoded.Length.ToString(CultureInfo.InvariantCulture));
            WriteAscii(output, ":");
            output.Write(encoded, 0, encoded.Length);
        }

        private static void AppendArray(Stream output, JArray value, string path)
        {
            WriteAscii(output, "a");
            WriteAscii(output, value.Count.ToString(CultureInfo.InvariantCulture));
            WriteAscii(output, ":");
            for (int index = 0; index < value.Count; index++)
            {
                AppendValue(output, value[index], path + "[" + index.ToString(CultureInfo.InvariantCulture) + "]");
            }
        }

        private static void AppendObject(Stream output, JObject value, string path)
        {
            List<EncodedProperty> properties = new List<EncodedProperty>();
            HashSet<string> names = new HashSet<string>(StringComparer.Ordinal);
            foreach (JProperty property in value.Properties())
            {
                if (!names.Add(property.Name))
                {
                    throw new CanonicalHashException(
                        path + " contains duplicate object key " + property.Name + ".");
                }
                properties.Add(new EncodedProperty(
                    property,
                    EncodeUtf8(property.Name, path + " object key")));
            }

            properties.Sort(delegate(EncodedProperty left, EncodedProperty right)
            {
                return CompareBytes(left.NameBytes, right.NameBytes);
            });

            WriteAscii(output, "o");
            WriteAscii(output, properties.Count.ToString(CultureInfo.InvariantCulture));
            WriteAscii(output, ":");
            foreach (EncodedProperty property in properties)
            {
                AppendEncodedString(output, property.NameBytes);
                AppendValue(output, property.Property.Value, path + "." + property.Property.Name);
            }
        }

        private static int CompareBytes(byte[] left, byte[] right)
        {
            int common = Math.Min(left.Length, right.Length);
            for (int index = 0; index < common; index++)
            {
                int comparison = left[index].CompareTo(right[index]);
                if (comparison != 0)
                {
                    return comparison;
                }
            }
            return left.Length.CompareTo(right.Length);
        }

        private static byte[] EncodeUtf8(string value, string path)
        {
            if (value == null)
            {
                throw new CanonicalHashException(path + " is null instead of a string.");
            }
            try
            {
                return StrictUtf8.GetBytes(value);
            }
            catch (EncoderFallbackException error)
            {
                throw new CanonicalHashException(path + " contains an unpaired Unicode surrogate.", error);
            }
        }

        private static void ValidateUnicode(string value, string path)
        {
            EncodeUtf8(value, path);
        }

        private static void WriteAscii(Stream output, string value)
        {
            byte[] encoded = Encoding.ASCII.GetBytes(value);
            output.Write(encoded, 0, encoded.Length);
        }

        private static byte[] BuildDomain()
        {
            byte[] identifier = Encoding.ASCII.GetBytes(ContractId);
            byte[] domain = new byte[identifier.Length + 1];
            Buffer.BlockCopy(identifier, 0, domain, 0, identifier.Length);
            return domain;
        }

        private sealed class EncodedProperty
        {
            internal EncodedProperty(JProperty property, byte[] nameBytes)
            {
                Property = property;
                NameBytes = nameBytes;
            }

            internal JProperty Property { get; private set; }
            internal byte[] NameBytes { get; private set; }
        }

        private sealed class JsonScope
        {
            private JsonScope(bool isObject)
            {
                IsObject = isObject;
                Names = isObject ? new HashSet<string>(StringComparer.Ordinal) : null;
            }

            internal bool IsObject { get; private set; }
            internal HashSet<string> Names { get; private set; }

            internal static JsonScope Object()
            {
                return new JsonScope(true);
            }

            internal static JsonScope Array()
            {
                return new JsonScope(false);
            }
        }
    }

    internal sealed class CanonicalHashException : Exception
    {
        internal CanonicalHashException(string message)
            : base(message)
        {
        }

        internal CanonicalHashException(string message, Exception innerException)
            : base(message, innerException)
        {
        }
    }
}
