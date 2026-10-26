using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace OniAiProbeMod.Probe
{
    /// <summary>
    /// Reads the working state and stored contents of the life-support buildings
    /// the controller can actually place.
    ///
    /// The full ``functional_building_*`` family is opt-in because it enumerates
    /// every completed building and made up about 78% of each snapshot.  That is
    /// far too expensive to leave on, and without it there was no way to answer
    /// the one question that mattered: the three oxygen diffusers the loop built
    /// all reported ``operational: false`` and nothing explained why (2026-10-26
    /// rounds 28-31).
    ///
    /// This reader is deliberately cheap enough to always run: it filters to a
    /// tiny prefab allowlist, so it touches a handful of buildings instead of the
    /// whole world, and it reports only what distinguishes the possible causes —
    /// the building's own operational/active/functional flags and what its
    /// storage actually holds.
    /// </summary>
    internal static class FunctionalBuildingLifeSupportTelemetryReader
    {
        internal const string Schema = "oni.functional_building_life_support.v1";
        internal const string Source =
            "Components.BuildingCompletes.GetWorldItems(activeWorldId)+Operational.IsOperational/IsActive/IsFunctional"
            + "+Storage.GetItems/PrimaryElement.ElementID/Mass"
            + "+ManualDeliveryKG.RequestedItemTag/Capacity/IsPaused"
            + "+ElementConsumer.IsElementAvailable/AverageConsumeRate";

        /// <summary>Prefabs whose working state the controller depends on.</summary>
        internal static readonly string[] TrackedPrefabIds =
        {
            "MineralDeoxidizer",
            "AlgaeHabitat",
            "Electrolyzer",
        };

        private const int EnumerationLimit = 10000;
        private const int StoredItemLimit = 24;
        /// <summary>How many pickupables of one tag may be tested for reachability.</summary>
        private const int MaterialSampleLimit = 512;

        internal static Dictionary<string, object> Read()
        {
            DateTimeOffset capturedAt = DateTimeOffset.UtcNow;
            List<Dictionary<string, object>> values = new List<Dictionary<string, object>>();
            int activeWorldId = -1;

            try
            {
                ClusterManager manager = ClusterManager.Instance;
                if (manager == null)
                {
                    return Error(capturedAt, activeWorldId, values, "cluster_manager_unavailable");
                }

                WorldContainer activeWorld = manager.activeWorld;
                activeWorldId = manager.activeWorldId;
                if (activeWorld == null)
                {
                    return Error(capturedAt, activeWorldId, values, "active_world_unavailable");
                }

                List<Dictionary<string, object>> materials = ReadWorldMaterials(activeWorld);

                IEnumerable<BuildingComplete> items;
                try
                {
                    items = Components.BuildingCompletes.GetWorldItems(activeWorldId);
                }
                catch
                {
                    return Error(capturedAt, activeWorldId, values, "building_registry_error");
                }
                if (items == null)
                {
                    return Error(capturedAt, activeWorldId, values, "building_registry_unavailable");
                }

                int seen = 0;
                foreach (BuildingComplete item in items)
                {
                    if (seen++ >= EnumerationLimit)
                    {
                        return Error(capturedAt, activeWorldId, values, "building_registry_limit");
                    }
                    if (item == null || item.gameObject == null || item.Def == null)
                    {
                        continue;
                    }
                    string prefabId = item.Def.PrefabID;
                    if (string.IsNullOrEmpty(prefabId) || Array.IndexOf(TrackedPrefabIds, prefabId) < 0)
                    {
                        continue;
                    }
                    int itemWorld;
                    try
                    {
                        itemWorld = item.gameObject.GetMyWorldId();
                    }
                    catch
                    {
                        continue;
                    }
                    if (itemWorld != activeWorldId)
                    {
                        continue;
                    }
                    KPrefabID prefab = item.GetComponent<KPrefabID>();
                    if (prefab == null || prefab.InstanceID <= 0)
                    {
                        continue;
                    }
                    values.Add(Describe(item, prefab.InstanceID, itemWorld, prefabId, activeWorld));
                }

                values.Sort(Compare);
                return Payload(capturedAt, activeWorldId, true, values, "observed", null, materials);
            }
            catch (Exception ex)
            {
                return Error(capturedAt, activeWorldId, values, "life_support_read_error:" + ex.GetType().Name);
            }
        }

        private static Dictionary<string, object> Describe(
            BuildingComplete item, int instanceId, int worldId, string prefabId, WorldContainer world)
        {
            Dictionary<string, object> value = new Dictionary<string, object>
            {
                { "instance_id", instanceId },
                { "prefab_id", prefabId },
                { "world_id", worldId },
            };

            Operational operational = item.GetComponent<Operational>();
            value["operational"] = operational == null ? (object)null : operational.IsOperational;
            value["active"] = operational == null ? (object)null : operational.IsActive;
            value["functional"] = operational == null ? (object)null : operational.IsFunctional;

            BuildingEnabledButton enabled = item.GetComponent<BuildingEnabledButton>();
            value["enabled"] = enabled == null ? (object)null : enabled.IsEnabled;

            Storage storage = item.GetComponent<Storage>();
            if (storage == null)
            {
                value["storage"] = null;
                value["stored_item_count"] = null;
                return value;
            }

            // What the building wants delivered, and whether the world can supply
            // it.  An empty storage alone does not say whether the consumable is
            // missing from the world or simply never fetched (round 32).
            ManualDeliveryKG delivery = item.GetComponent<ManualDeliveryKG>();
            if (delivery == null)
            {
                value["requested_item_tag"] = null;
                value["delivery_capacity_kg"] = null;
                value["delivery_paused"] = null;
            }
            else
            {
                value["requested_item_tag"] = delivery.RequestedItemTag.ToString();
                value["delivery_capacity_kg"] = delivery.Capacity;
                value["delivery_paused"] = delivery.IsPaused;
                // The world's own inventory answers the question an empty storage
                // cannot: is the consumable missing, or merely never fetched?
                try
                {
                    WorldInventory inventory = world == null ? null : world.worldInventory;
                    value["requested_item_available_kg"] = inventory == null
                        ? (object)null
                        : inventory.GetAmount(delivery.RequestedItemTag, false);
                    value["inventory_valid"] = inventory == null ? (object)null : inventory.HasValidCount;
                }
                catch
                {
                    value["requested_item_available_kg"] = null;
                    value["inventory_valid"] = null;
                }
            }

            ElementConsumer consumer = item.GetComponent<ElementConsumer>();
            if (consumer == null)
            {
                value["consumer_element_available"] = null;
                value["consumer_average_rate"] = null;
            }
            else
            {
                value["consumer_element_available"] = consumer.IsElementAvailable;
                value["consumer_average_rate"] = consumer.AverageConsumeRate;
            }

            List<Dictionary<string, object>> stored = new List<Dictionary<string, object>>();
            try
            {
                List<GameObject> contents = storage.GetItems();
                if (contents != null)
                {
                    for (int index = 0; index < contents.Count && index < StoredItemLimit; index++)
                    {
                        GameObject entry = contents[index];
                        if (entry == null)
                        {
                            continue;
                        }
                        PrimaryElement element = entry.GetComponent<PrimaryElement>();
                        KPrefabID entryPrefab = entry.GetComponent<KPrefabID>();
                        stored.Add(new Dictionary<string, object>
                        {
                            { "tag", entryPrefab == null ? null : entryPrefab.PrefabTag.ToString() },
                            { "element_id", element == null ? null : element.ElementID.ToString() },
                            { "mass_kg", element == null ? (object)null : element.Mass },
                        });
                    }
                    value["stored_item_count"] = contents.Count;
                }
                else
                {
                    value["stored_item_count"] = null;
                }
            }
            catch
            {
                value["stored_item_count"] = null;
            }
            value["storage"] = stored;
            return value;
        }

        /// <summary>
        /// The world's own inventory for the materials the loop's buildings need,
        /// split into what exists and what a duplicant could actually fetch.
        ///
        /// A placed blueprint reported ``available_mass_kg`` well above its
        /// requirement and still never received a single kilogram, while every
        /// duplicant was idle: the amount the catalog reports is world-wide, and a
        /// fetch chore is only created for material a duplicant can reach
        /// (2026-10-26 round 46).
        /// </summary>
        private static List<Dictionary<string, object>> ReadWorldMaterials(WorldContainer world)
        {
            List<Dictionary<string, object>> results = new List<Dictionary<string, object>>();
            WorldInventory inventory = world == null ? null : world.worldInventory;
            if (inventory == null)
            {
                return results;
            }
            string[] tags = { "Cinnabar", "Dirt", "Algae", "Water", "Sandstone" };
            foreach (string name in tags)
            {
                Dictionary<string, object> entry = new Dictionary<string, object>
                {
                    { "tag", name },
                    { "amount_kg", null },
                    { "reachable_kg", null },
                    { "pickupable_count", null },
                    { "sampled_count", null },
                };
                try
                {
                    Tag tag = TagManager.Create(name);
                    entry["amount_kg"] = inventory.GetAmount(tag, false);
                    ICollection<Pickupable> items = inventory.GetPickupables(tag, false);
                    if (items != null)
                    {
                        entry["pickupable_count"] = items.Count;
                        double reachable = 0.0;
                        int sampled = 0;
                        foreach (Pickupable item in items)
                        {
                            if (item == null || sampled >= MaterialSampleLimit)
                            {
                                break;
                            }
                            sampled++;
                            if (!inventory.IsReachable(item))
                            {
                                continue;
                            }
                            PrimaryElement element = item.GetComponent<PrimaryElement>();
                            if (element != null)
                            {
                                reachable += element.Mass;
                            }
                        }
                        entry["sampled_count"] = sampled;
                        entry["reachable_kg"] = Math.Round(reachable, 3);
                    }
                }
                catch
                {
                    // Leave the nulls: an unreadable tag is reported as unread.
                }
                results.Add(entry);
            }
            return results;
        }

        private static int Compare(Dictionary<string, object> left, Dictionary<string, object> right)
        {
            int a = left.TryGetValue("instance_id", out object x) && x is int xi ? xi : 0;
            int b = right.TryGetValue("instance_id", out object y) && y is int yi ? yi : 0;
            return a.CompareTo(b);
        }

        private static Dictionary<string, object> Payload(
            DateTimeOffset at, int worldId, bool complete, List<Dictionary<string, object>> values,
            string status, string failureReason,
            List<Dictionary<string, object>> worldMaterials = null)
        {
            return new Dictionary<string, object>
            {
                { "schema", Schema },
                { "source", Source },
                { "captured_at", at.ToString("o", CultureInfo.InvariantCulture) },
                { "world_id", worldId },
                { "complete", complete },
                { "status", status },
                { "failure_reason", failureReason },
                { "tracked_prefab_ids", TrackedPrefabIds },
                { "instances", values },
                { "world_materials", worldMaterials ?? new List<Dictionary<string, object>>() },
            };
        }

        private static Dictionary<string, object> Error(
            DateTimeOffset at, int worldId, List<Dictionary<string, object>> values, string reason)
        {
            return Payload(at, worldId, false, values, "unsupported", reason);
        }
    }
}
