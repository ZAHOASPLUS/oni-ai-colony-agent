using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>Individual ownership and massage settings, through the game's ordinary side-screen APIs.</summary>
    internal static class ApiBuildingSettings
    {
        internal static bool Configure(GameObject building, JObject args, Dictionary<string, object> applied)
        {
            bool setAssignment = args.Property("assigned_duplicant") != null;
            bool setMassage = args.Property("massage_start_stress") != null || args.Property("massage_stop_stress") != null;
            if (!setAssignment && !setMassage)
            {
                return false;
            }
            // Not a blueprint: a map feature such as a Gravitas teleporter has no
            // BuildingComplete but is assigned the same way.
            if (building.GetComponent<BuildingUnderConstruction>() != null)
            {
                throw new ApiException("unsupported_setting", "These settings need a finished building.");
            }

            // Validate both new settings before changing either one.
            Assignable assignable = null;
            MinionAssignablesProxy target = null;
            if (setAssignment)
            {
                string requested = ReadAssignment(args["assigned_duplicant"]);
                assignable = building.GetComponent<Assignable>();
                if (assignable == null || !assignable.CanBeAssigned || building.GetComponent<AssignmentGroupController>() != null)
                {
                    throw new ApiException("unsupported_setting", "This building has no individual assignment side screen.");
                }
                // AssignableSideScreen.Refresh only offers the room for secondary
                // room furniture. Do not override the game's automatic room owner.
                Room room = Game.Instance.roomProber.GetRoomOfGameObject(building);
                if (building.GetComponent<Equippable>() == null &&
                    !building.GetComponent<KPrefabID>().HasTag(GameTags.NotRoomAssignable) &&
                    room?.roomType.primary_constraint != null &&
                    !room.roomType.primary_constraint.building_criteria(building.GetComponent<KPrefabID>()))
                {
                    throw new ApiException("assignment_refused", "This building is assigned through its room, not to an individual.");
                }
                if (requested.Length > 0)
                {
                    MinionIdentity identity = ApiDuplicants.Find(new JValue(requested), "assigned_duplicant");
                    target = identity.assignableProxy?.Get();
                    if (target == null || !assignable.CanAssignTo(target))
                    {
                        throw new ApiException("assignment_refused", "The game does not allow this duplicant to be assigned here.");
                    }
                }
            }

            MassageTable massage = null;
            float[] range = null;
            if (setMassage)
            {
                massage = building.GetComponent<MassageTable>();
                if (massage == null)
                {
                    throw new ApiException("unsupported_setting", "Stress thresholds apply only to a MassageTable.");
                }
                range = ReadMassageRange(args, massage.ActivateValue, massage.DeactivateValue);
            }

            if (setAssignment)
            {
                // AssignableSideScreen.ChangeAssignment uses exactly this sequence.
                // Unassign restores public use when the building allows it.
                if (target == null || !ReferenceEquals(assignable.assignee, target))
                {
                    assignable.Unassign();
                    if (target != null)
                    {
                        assignable.Assign(target);
                        if (!ReferenceEquals(assignable.assignee, target))
                        {
                            throw new ApiException("assignment_refused", "The game did not apply the requested assignment.");
                        }
                    }
                }
                applied["assignment"] = DescribeAssignment(assignable);
            }
            if (setMassage)
            {
                ApplyMassageRange(massage, range);
                applied["massage_start_stress"] = massage.ActivateValue;
                applied["massage_stop_stress"] = massage.DeactivateValue;
            }
            return true;
        }

        internal static void Describe(GameObject building, Dictionary<string, object> entry)
        {
            Assignable assignable = building.GetComponent<Assignable>();
            if (assignable != null)
            {
                entry["assignment"] = DescribeAssignment(assignable);
            }
            MassageTable massage = building.GetComponent<MassageTable>();
            if (massage != null)
            {
                entry["massage_start_stress"] = massage.ActivateValue;
                entry["massage_stop_stress"] = massage.DeactivateValue;
            }
        }

        private static Dictionary<string, object> DescribeAssignment(Assignable assignable)
        {
            IAssignableIdentity owner = assignable.assignee;
            MinionAssignablesProxy proxy = owner as MinionAssignablesProxy;
            MinionIdentity identity = owner as MinionIdentity;
            if (proxy != null)
            {
                identity = proxy.GetTargetGameObject()?.GetComponent<MinionIdentity>();
            }
            AssignmentGroup group = owner as AssignmentGroup;
            return new Dictionary<string, object>
            {
                { "kind", owner == null ? "unassigned" : proxy != null || identity != null ? "duplicant" :
                    group != null ? group.id == "public" ? "public" : "group" : owner is Room ? "room" : "other" },
                { "id", identity != null ? (object)ApiDuplicants.Id(identity) : proxy != null ? (object)proxy.TargetInstanceID : null },
                { "name", owner?.GetProperName() },
                { "can_be_assigned", assignable.CanBeAssigned },
                { "can_be_public", assignable.canBePublic }
            };
        }

        internal static string ReadAssignment(JToken value)
        {
            if (value == null || value.Type != JTokenType.String)
            {
                throw new ApiException("invalid_argument", "assigned_duplicant must be an id or unique name as a string, or an empty string to release it.");
            }
            return value.Value<string>();
        }

        // Kept independent of Unity so partial updates and refusal-before-write
        // can be checked offline against the same code the tool executes.
        internal static float[] ReadMassageRange(JObject args, float currentStart, float currentStop)
        {
            float start = ReadStress(args, "massage_start_stress", currentStart);
            float stop = ReadStress(args, "massage_stop_stress", currentStop);
            if (stop > start)
            {
                throw new ApiException("invalid_argument", "massage_stop_stress must not exceed massage_start_stress; supply both to change the range.");
            }
            return new[] { start, stop };
        }

        private static float ReadStress(JObject args, string name, float fallback)
        {
            JToken value = args[name];
            if (value == null)
            {
                return fallback;
            }
            if (value.Type != JTokenType.Integer || value.Value<double>() < 0 || value.Value<double>() > 100)
            {
                throw new ApiException("invalid_argument", name + " must be an integer from 0 to 100.");
            }
            return value.Value<int>();
        }

        internal static void ApplyMassageRange(MassageTable table, float[] range)
        {
            // ActiveRangeSideScreen writes these same properties. The final
            // range has already passed its stop <= start and 0..100 limits.
            table.ActivateValue = range[0];
            table.DeactivateValue = range[1];
        }
    }
}
