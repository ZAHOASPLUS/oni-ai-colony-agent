using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace OniAiProbeMod.Api
{
    /// <summary>The errands and settings a colony needs to feed and run itself.</summary>
    internal static class ApiPlayTools
    {
        // MopTool.maxMopAmt: deeper liquid has to be pumped.
        private const float MaxMopKg = 150f;
        private const int ChorePriorityMax = 5;
        private const string ScheduleBlockChars = "HWRS";

        internal static object Harvest(JObject args)
        {
            HashSet<int> cells = new HashSet<int>(ApiActions.ReadArea(args));
            int priority = ApiActions.ReadPriority(args);
            bool enable = ApiArgs.Flag(args, "enable", true);
            int changed = 0;
            int ready = 0;
            foreach (HarvestDesignatable plant in Components.HarvestDesignatables.Items)
            {
                if (plant == null || !Occupies(plant, cells))
                {
                    continue;
                }
                if (!enable)
                {
                    plant.GetComponent<Harvestable>()?.Trigger((int)GameHashes.Cancel);
                }
                plant.SetHarvestWhenReady(enable);
                ApiActions.SetPriority(plant.gameObject, priority);
                changed++;
                if (plant.CanBeHarvested())
                {
                    ready++;
                }
            }
            return new Dictionary<string, object>
            {
                { "plants", changed },
                { "ready_now", ready },
                { "harvest_when_ready", enable }
            };
        }

        private static bool Occupies(HarvestDesignatable plant, HashSet<int> cells)
        {
            return cells.Contains(Grid.PosToCell(plant)) ||
                (plant.area != null && cells.Any(cell => plant.area.CheckIsOccupying(cell)));
        }

        internal static object Sweep(JObject args)
        {
            int priority = ApiActions.ReadPriority(args);
            int marked = 0;
            foreach (int cell in ApiActions.ReadArea(args))
            {
                GameObject first = Grid.Objects[cell, (int)ObjectLayer.Pickupables];
                ObjectLayerListItem node = first == null ? null : first.GetComponent<Pickupable>()?.objectLayerListItem;
                while (node != null)
                {
                    Pickupable pickupable = node.pickupable;
                    GameObject item = node.gameObject;
                    node = node.nextItem;
                    if (item != null &&
                        !pickupable.KPrefabID.HasTag(GameTags.BaseMinion) &&
                        pickupable.Clearable.isClearable)
                    {
                        pickupable.Clearable.MarkForClear();
                        ApiActions.SetPriority(item, priority);
                        marked++;
                    }
                }
            }
            return new Dictionary<string, object> { { "items_marked", marked } };
        }

        internal static object Mop(JObject args)
        {
            int priority = ApiActions.ReadPriority(args);
            int marked = 0;
            int tooDeep = 0;
            int noFloor = 0;
            foreach (int cell in ApiActions.ReadArea(args))
            {
                if (Grid.Solid[cell] ||
                    !Grid.Element[cell].IsLiquid ||
                    Grid.Objects[cell, (int)ObjectLayer.MopPlacer] != null)
                {
                    continue;
                }
                int below = Grid.CellBelow(cell);
                if (!Grid.IsValidCell(below) || !Grid.Solid[below])
                {
                    noFloor++;
                    continue;
                }
                if (Grid.Mass[cell] > MaxMopKg)
                {
                    tooDeep++;
                    continue;
                }

                // The same steps as MopTool.OnDragTool.
                GameObject placer = Util.KInstantiate(Assets.GetPrefab(new Tag(MopPlacerConfig.ID)));
                Grid.Objects[cell, (int)ObjectLayer.MopPlacer] = placer;
                Vector3 position = Grid.CellToPosCBC(cell, Grid.SceneLayer.FXFront);
                position.z -= 0.15f;
                placer.transform.SetPosition(position);
                placer.SetActive(true);
                ApiActions.SetPriority(placer, priority);
                marked++;
            }
            return new Dictionary<string, object>
            {
                { "marked", marked },
                { "too_deep", tooDeep },
                { "no_floor", noFloor }
            };
        }

        internal static object Configure(JObject args)
        {
            GameObject building = ApiActions.FindById(ApiArgs.Int(args, "id", 0));
            Dictionary<string, object> applied = new Dictionary<string, object>();
            bool any = false;
            string recipe = ApiArgs.Text(args, "recipe");
            if (recipe != null)
            {
                applied["recipe"] = SetRecipe(building, recipe, ApiArgs.Int(args, "count", 1));
                any = true;
            }
            string seed = ApiArgs.Text(args, "seed");
            if (seed != null)
            {
                applied["seed"] = SetSeed(building, seed);
                any = true;
            }
            if (args["storage_filter"] is JArray filter)
            {
                applied["storage_filter"] = SetStorageFilter(building, filter);
                any = true;
            }
            string elementFilter = ApiArgs.Text(args, "element_filter");
            if (elementFilter != null)
            {
                applied["element_filter"] = SetElementFilter(building, elementFilter);
                any = true;
            }
            if (args["crew"] is JArray crew)
            {
                applied["crew"] = ApiRocketTools.SetCrew(building, crew);
                any = true;
            }
            string rocketRestriction = ApiArgs.Text(args, "rocket_restriction");
            if (rocketRestriction != null)
            {
                applied["rocket_restriction"] = ApiRocketTools.SetRestriction(building, rocketRestriction);
                any = true;
            }
            string rocketAction = ApiArgs.Text(args, "rocket_action");
            if (rocketAction != null)
            {
                applied["rocket_action"] = ApiRocketTools.ExecuteRocketAction(building, rocketAction);
                any = true;
            }
            any = ApiBuildingSettings.Configure(building, args, applied) || any;
            any = ApiLogic.Configure(building, args, applied) || any;
            any = ApiGeysers.Configure(building, args, applied) || any;
            any = ApiCritters.Configure(building, args, applied) || any;
            any = ApiSuits.Configure(building, args, applied) || any;
            if (!any)
            {
                throw new ApiException(
                    "invalid_argument",
                    "Give recipe, seed, storage_filter, element_filter, crew, rocket_restriction, rocket_action, assigned_duplicant, massage stress thresholds, smart battery thresholds, study, capture, critter_limit or request_suit.");
            }
            return applied;
        }

        private static object SetElementFilter(GameObject building, string name)
        {
            Filterable filterable = building.GetComponent<Filterable>();
            if (filterable == null)
            {
                throw new ApiException("unsupported_setting", "This building filters no element.");
            }
            Element element = ElementLoader.FindElementByName(name);
            if (element == null)
            {
                throw new ApiException("unknown_element", name + " is not an element.");
            }
            filterable.SelectedTag = element.tag;
            return name;
        }

        private static object SetRecipe(GameObject building, string id, int count)
        {
            ComplexFabricator fabricator = building.GetComponent<ComplexFabricator>();
            if (fabricator == null)
            {
                throw new ApiException("unsupported_setting", "This building makes nothing from recipes.");
            }
            ComplexRecipe recipe = fabricator.GetRecipes().FirstOrDefault(candidate => candidate.id == id);
            if (recipe == null)
            {
                throw new ApiException("unknown_recipe", "This building has no recipe " + id + ".", "inspect lists its recipes.");
            }
            if (count != ComplexFabricator.QUEUE_INFINITE && (count < 0 || count > ComplexFabricator.MAX_QUEUE_SIZE))
            {
                throw new ApiException(
                    "invalid_argument",
                    "count must be 0 to " + ComplexFabricator.MAX_QUEUE_SIZE + ", or -1 to repeat forever.");
            }
            fabricator.SetRecipeQueueCount(recipe, count);
            return new Dictionary<string, object> { { "id", id }, { "queued", fabricator.GetRecipeQueueCount(recipe) } };
        }

        private static object SetSeed(GameObject building, string name)
        {
            SingleEntityReceptacle receptacle = building.GetComponent<SingleEntityReceptacle>();
            if (receptacle == null)
            {
                throw new ApiException("unsupported_setting", "Nothing can be planted in this building.");
            }
            if (receptacle.Occupant != null)
            {
                throw new ApiException("occupied", "Something is already planted here.", "Deconstruct and rebuild it, or plant elsewhere.");
            }
            Tag tag = new Tag(name);
            GameObject prefab = Assets.TryGetPrefab(tag);
            if (prefab == null || !receptacle.IsValidEntity(prefab))
            {
                throw new ApiException("invalid_seed", name + " cannot be planted here.", "inspect lists the seeds the colony has for it.");
            }
            receptacle.CreateOrder(tag, Tag.Invalid);
            if (receptacle.GetActiveRequest == null)
            {
                throw new ApiException("invalid_seed", "The game refused " + name + " here.");
            }
            return name;
        }

        private static object SetStorageFilter(GameObject building, JArray names)
        {
            TreeFilterable filterable = building.GetComponent<TreeFilterable>();
            if (filterable == null)
            {
                throw new ApiException("unsupported_setting", "This building has no storage filter.");
            }
            HashSet<Tag> tags = new HashSet<Tag>(names.Select(name => new Tag(name.ToString())));
            filterable.UpdateFilters(tags);
            return tags.Select(tag => tag.Name).ToList();
        }

        internal static object SetChorePriority(JObject args)
        {
            MinionIdentity identity = ApiDuplicants.Read(args, "duplicant");
            string groupId = ApiArgs.Text(args, "chore_group");
            ChoreGroup group = Db.Get().ChoreGroups.resources
                .FirstOrDefault(candidate => string.Equals(candidate.Id, groupId, StringComparison.OrdinalIgnoreCase));
            if (group == null)
            {
                throw new ApiException(
                    "unknown_chore_group",
                    "No chore group is called " + groupId + ".",
                    "Groups: " + string.Join(", ", Db.Get().ChoreGroups.resources.Select(candidate => candidate.Id)));
            }
            int level = ApiArgs.Int(args, "level", -1);
            if (level < 0 || level > ChorePriorityMax)
            {
                throw new ApiException("invalid_argument", "level must be 0 (never) to " + ChorePriorityMax + ".");
            }

            ChoreConsumer consumer = identity.GetComponent<ChoreConsumer>();
            consumer.SetPersonalPriority(group, level);
            return new Dictionary<string, object>
            {
                { "duplicant", identity.GetProperName() },
                { "id", ApiDuplicants.Id(identity) },
                { "chore_group", group.Id },
                { "level", consumer.GetPersonalPriority(group) }
            };
        }

        internal static object SetSchedule(JObject args)
        {
            List<Schedule> schedules = ScheduleManager.Instance.GetSchedules();
            int index = ApiArgs.Int(args, "schedule", 0);
            if (index < 0 || index >= schedules.Count)
            {
                throw new ApiException("invalid_argument", "schedule must be 0 to " + (schedules.Count - 1) + ".");
            }
            Schedule schedule = schedules[index];
            string blocks = ApiArgs.Text(args, "blocks");
            int expected = schedule.GetBlocks().Count;
            if (blocks == null || blocks.Length != expected || blocks.Any(block => ScheduleBlockChars.IndexOf(block) < 0))
            {
                throw new ApiException(
                    "invalid_argument",
                    "blocks must be " + expected + " characters, each H (hygiene), W (work), R (recreation) or S (sleep).",
                    "get_status shows the current schedules in this form.");
            }
            for (int block = 0; block < blocks.Length; block++)
            {
                schedule.SetBlockGroup(block, GroupFor(blocks[block]));
            }
            return new Dictionary<string, object> { { "schedule", index }, { "blocks", Describe(schedule) } };
        }

        private static ScheduleGroup GroupFor(char block)
        {
            Database.ScheduleGroups groups = Db.Get().ScheduleGroups;
            switch (block)
            {
                case 'H':
                    return groups.Hygene;
                case 'W':
                    return groups.Worktime;
                case 'R':
                    return groups.Recreation;
                default:
                    return groups.Sleep;
            }
        }

        internal static string Describe(Schedule schedule)
        {
            Database.ScheduleGroups groups = Db.Get().ScheduleGroups;
            return new string(schedule.GetBlocks().Select(block =>
                block.GroupId == groups.Hygene.Id ? 'H' :
                block.GroupId == groups.Worktime.Id ? 'W' :
                block.GroupId == groups.Recreation.Id ? 'R' : 'S').ToArray());
        }
    }
}
