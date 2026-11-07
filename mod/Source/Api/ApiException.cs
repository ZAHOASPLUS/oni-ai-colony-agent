using System;
using Newtonsoft.Json.Linq;

namespace OniAiProbeMod.Api
{
    /// <summary>A refusal the caller can act on; it becomes the response's error object.</summary>
    internal sealed class ApiException : Exception
    {
        internal ApiException(string code, string message, string hint = null, JObject details = null)
            : base(message)
        {
            Code = code;
            Hint = hint;
            Details = details;
        }

        internal string Code { get; }

        internal string Hint { get; }

        /// <summary>More fields for the error object, such as the ids an ambiguous name could mean.</summary>
        internal JObject Details { get; }
    }

}
