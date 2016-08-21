import copy
import json
import math
import os

from pokemongo_bot import inventory
from pokemongo_bot.base_dir import _base_dir
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.datastore import Datastore
from pokemongo_bot.human_behaviour import sleep, action_delay
from pokemongo_bot.item_list import Item
from pokemongo_bot.worker_result import WorkerResult

SUCCESS = 1
ERROR_INVALID_ITEM_TYPE = 2
ERROR_XP_BOOST_ALREADY_ACTIVE = 3
ERROR_NO_ITEMS_REMAINING = 4
ERROR_LOCATION_UNSET = 5


class PokemonOptimizer(Datastore, BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(PokemonOptimizer, self).__init__(bot, config)

    def initialize(self):
        self.family_by_family_id = {}
        self.max_pokemon_storage = inventory.get_pokemon_inventory_size()
        self.last_pokemon_count = 0

        self.config_transfer = self.config.get("transfer", False)
        self.config_evolve = self.config.get("evolve", False)
        self.config_upgrade = self.config.get("upgrade", False)
        self.config_evolve_time = self.config.get("evolve_time", 20)
        self.config_evolve_for_xp = self.config.get("evolve_for_xp", True)
        self.config_pokemon_for_xp = self.config.get("pokemon_for_xp", ["Rattata", "Pidgey", "Weedle", "Zubat", "Caterpie"])
        self.config_evolve_only_with_lucky_egg = self.config.get("evolve_only_with_lucky_egg", False)
        self.config_evolve_count_for_lucky_egg = self.config.get("evolve_count_for_lucky_egg", 92)
        self.config_may_use_lucky_egg = self.config.get("may_use_lucky_egg", False)
        self.config_keep = self.config.get("keep", [{"top": 1, "sort": ["cp"]},
                                                    {"top": 1, "sort": ["ncp", "iv"]},
                                                    {"top": 1, "sort": ["iv"]},
                                                    {"top": 1, "sort": ["ncp", "iv"], "min": {"iv": 0.9}, "evolve": True},
                                                    {"top": 1, "sort": ["ncp", "iv"], "min": {"iv": 0.9, "moveset.attack_perfection": 1.0}, "upgrade": True},
                                                    {"top": 1, "sort": ["ncp", "iv"], "min": {"iv": 0.9, "moveset.defense_perfection": 1.0}, "upgrade": True}])

        self.config_transfer_wait_min = self.config.get("transfer_wait_min", 1)
        self.config_transfer_wait_max = self.config.get("transfer_wait_max", 4)

        if (not self.config_may_use_lucky_egg) and self.config_evolve_only_with_lucky_egg:
            self.config_evolve = False

    def get_pokemon_slot_left(self):
        pokemon_count = inventory.Pokemons.get_space_used()

        if pokemon_count != self.last_pokemon_count:
            self.last_pokemon_count = pokemon_count
            self.logger.info("Pokemon Bag: %s/%s", pokemon_count, self.max_pokemon_storage)
            self.save_web_inventory()

        return inventory.Pokemons.get_space_left()

    def work(self):
        if (not self.enabled) or (self.get_pokemon_slot_left() > 5):
            return WorkerResult.SUCCESS

        self.open_inventory()

        transfer_all = []
        evo_all_best = []
        evo_all_crap = []
        upgrade_all = []

        for family_id, family in self.family_by_family_id.items():
            if family_id == 133:  # "Eevee"
                transfer, evo_best, evo_crap, upgrade = self.get_multi_family_optimized(family_id, family, 3)
            else:
                transfer, evo_best, evo_crap, upgrade = self.get_family_optimized(family_id, family)

            transfer_all += transfer
            evo_all_best += evo_best
            evo_all_crap += evo_crap
            upgrade_all += upgrade

        evo_all = evo_all_best + evo_all_crap

        upgrade_all = self.select_upgrades(upgrade_all)

        self.apply_optimization(transfer_all, evo_all, upgrade_all)
        self.save_web_inventory()

        return WorkerResult.SUCCESS

    def open_inventory(self):
        self.family_by_family_id.clear()

        for pokemon in inventory.pokemons().all():
            family_id = pokemon.first_evolution_id
            setattr(pokemon, "ncp", pokemon.cp_percent)
            setattr(pokemon, "dps", pokemon.moveset.dps)
            setattr(pokemon, "dps_attack", pokemon.moveset.dps_attack)
            setattr(pokemon, "dps_defense", pokemon.moveset.dps_defense)

            self.family_by_family_id.setdefault(family_id, []).append(pokemon)

    def save_web_inventory(self):
        web_inventory = os.path.join(_base_dir, "web", "inventory-%s.json" % self.bot.config.username)

        with open(web_inventory, "r") as infile:
            ii = json.load(infile)

        ii = [x for x in ii if not x.get("inventory_item_data", {}).get("pokedex_entry", None)]
        ii = [x for x in ii if not x.get("inventory_item_data", {}).get("candy", None)]
        ii = [x for x in ii if not x.get("inventory_item_data", {}).get("item", None)]
        ii = [x for x in ii if not x.get("inventory_item_data", {}).get("pokemon_data", None)]

        for pokedex in inventory.pokedex().all():
            ii.append({"inventory_item_data": {"pokedex_entry": pokedex}})

        for family_id, candy in inventory.candies()._data.items():
            ii.append({"inventory_item_data": {"candy": {"family_id": family_id, "candy": candy.quantity}}})

        for item_id, item in inventory.items()._data.items():
            ii.append({"inventory_item_data": {"item": {"item_id": item_id, "count": item.count}}})

        for pokemon in inventory.pokemons().all():
            ii.append({"inventory_item_data": {"pokemon_data": pokemon._data}})

        with open(web_inventory, "w") as outfile:
            json.dump(ii, outfile)

    def get_family_optimized(self, family_id, family):
        evolve_best = []
        keep_best = []
        upgrade_best = []
        family_names = self.get_family_names(family_id)

        for criteria in self.config_keep:
            names = criteria.get("names", [])

            if names and not any(n in family_names for n in names):
                continue

            best = self.get_top_rank(family, criteria)

            keep_best += best
            if criteria.get("evolve", False):
                evolve_best += best
            if criteria.get("upgrade", False):
                upgrade_best += best

        evolve_best = self.unique_pokemon(evolve_best)
        upgrade_best = self.unique_pokemon(upgrade_best)
        keep_best = self.unique_pokemon(keep_best)

        return self.get_evolution_plan(family_id, family, evolve_best, keep_best, upgrade_best)

    def get_multi_family_optimized(self, family_id, family, nb_branch):
        # Transfer each group of senior independently
        senior_family = [p for p in family if not p.has_next_evolution()]
        other_family = [p for p in family if p.has_next_evolution()]
        senior_pids = set(p.pokemon_id for p in senior_family)
        senior_grouped_family = {pid: [p for p in senior_family if p.pokemon_id == pid] for pid in senior_pids}

        if not self.config_evolve:
            transfer, evo_best, evo_crap, upgrade = self.get_family_optimized(family_id, other_family)
        elif len(senior_pids) < nb_branch:
            # We did not get every combination yet = All other Pokemon are potentially good to keep
            transfer, evo_best, evo_crap, upgrade = self.get_evolution_plan(family_id, [], other_family, [], [])
            evo_best.sort(key=lambda p: p.iv * p.ncp, reverse=True)
        else:
            evolve_best = []
            keep_best = []
            upgrade_best = []
            names = self.get_family_names(family_id)

            for criteria in self.config_keep:
                family_names = criteria.get("names", [])

                if names and not any(n in family_names for n in names):
                    continue

                top = []

                for f in senior_grouped_family.values():
                    top += self.get_top_rank(f, criteria)

                worst = self.get_sorted_family(top, criteria)[-1]
                best = self.get_better_rank(family, criteria, worst)

                keep_best += best
                if criteria.get("evolve", False):
                    evolve_best += best
                if criteria.get("upgrade", False):
                    upgrade_best += best

            evolve_best = self.unique_pokemon(evolve_best)
            upgrade_best = self.unique_pokemon(upgrade_best)
            keep_best = self.unique_pokemon(keep_best)
            transfer, evo_best, evo_crap, upgrade = self.get_evolution_plan(family_id, other_family, evolve_best, keep_best, upgrade_best)

        for senior_pid, senior_family in senior_grouped_family.items():
            transfer += self.get_family_optimized(senior_pid, senior_family)[0]

        return (transfer, evo_best, evo_crap, upgrade)

    def get_family_names(self, family_id):
        ids = [family_id]
        ids += inventory.Pokemons.data_for(family_id).next_evolutions_all[:]
        datas = [inventory.Pokemons.data_for(x) for x in ids]
        return [x.name for x in datas]

    def get_top_rank(self, family, criteria):
        sorted_family = self.get_sorted_family(family, criteria)
        index = criteria.get("top", 1) - 1

        if 0 <= index < len(sorted_family):
            worst = sorted_family[index]
            return [p for p in sorted_family if self.get_rank(p, criteria) >= self.get_rank(worst, criteria)]
        else:
            return sorted_family

    def get_better_rank(self, family, criteria, worst):
        return [p for p in self.get_sorted_family(family, criteria) if self.get_rank(p, criteria) >= self.get_rank(worst, criteria)]

    def get_sorted_family(self, family, criteria):
        return sorted(
            filter(lambda p: self.get_match_min_criteria(p, criteria), family),
            key=lambda p: self.get_rank(p, criteria), reverse=True
        )

    def get_rank(self, pokemon, criteria):
        return tuple(self.get_attr(pokemon, attr) for attr in criteria.get("sort"))

    def get_match_min_criteria(self, pokemon, criteria):
        for attr, min_value in criteria.get("min", {}).iteritems():
            value = self.get_attr(pokemon, attr)
            if value is None or value < min_value:
                return False

        return True

    def get_attr(self, obj, attr):
        for name in attr.split("."):
            if obj is not None:
                obj = getattr(obj, name, None)

        return obj

    def get_pokemon_max_cp(self, pokemon_name):
        return int(self.pokemon_max_cp.get(pokemon_name, 0))

    def unique_pokemon(self, l):
        seen = set()
        return [p for p in l if not (p.unique_id in seen or seen.add(p.unique_id))]

    def get_evolution_plan(self, family_id, family, evolve_best, keep_best, upgrade_best):
        candies = inventory.candies().get(family_id).quantity

        # All the rest is crap, for now
        crap = family[:]
        crap = [p for p in crap if p not in keep_best]
        crap = [p for p in crap if not p.in_fort and not p.is_favorite]
        crap.sort(key=lambda p: p.iv * p.ncp, reverse=True)

        # We will gain a candy whether we choose to transfer or evolve these Pokemon
        candies += len(crap)

        # Let's see if we can evolve our best Pokemon
        can_evolve_best = []

        for pokemon in evolve_best:
            if not pokemon.has_next_evolution():
                continue

            candies -= pokemon.evolution_cost

            if candies < 0:
                break

            candies += 1
            can_evolve_best.append(pokemon)

            # Not sure if the evo keep the same id
            next_pid = pokemon.next_evolution_ids[0]
            next_evo = copy.copy(pokemon)
            next_evo.pokemon_id = next_pid
            next_evo.static = inventory.pokemons().data_for(next_pid)
            next_evo.name = inventory.pokemons().name_for(next_pid)
            evolve_best.append(next_evo)

        # Let's see if we can power up our best Pokemon
        can_upgrade_best = []

        for pokemon in upgrade_best:
            if pokemon.has_next_evolution():
                continue

            self.logger.debug("Checking %s for power-up" % pokemon)

            total_evolution_cost = 0
            pokemon_id = family_id
            while inventory.pokemons().has_next_evolution(pokemon_id):
                total_evolution_cost += inventory.pokemons().evolution_cost_for(pokemon_id)
                pokemon_id = inventory.pokemons().next_evolution_ids_for(pokemon_id)[0]
            total_evolution_cost = 0

            upgrade_to = pokemon.level
            while upgrade_to < min(inventory.player_stats().level + 1.5, 41):
                candy_cost, stardust_cost = self.get_upgrade_cost(upgrade_to)
                if candies < total_evolution_cost + candy_cost:
                    break
                upgrade_to += 0.5
                candies -= candy_cost

            if upgrade_to > pokemon.level:
                can_upgrade_best.append((pokemon, int((upgrade_to - pokemon.level) * 2)))
                self.logger.debug("Enough candy for %d power-ups", int((upgrade_to - pokemon.level) * 2))
            else:
                self.logger.debug("Not enough candy")

        if self.config_evolve_for_xp and self.use_pokemon_for_xp(family_id):
            # Compute how many crap we should keep if we want to batch evolve them for xp
            junior_evolution_cost = inventory.pokemons().evolution_cost_for(family_id)

            # transfer + keep_for_evo = len(crap)
            # leftover_candies = candies - len(crap) + transfer * 1
            # keep_for_evo = (leftover_candies - 1) / (junior_evolution_cost - 1)
            # keep_for_evo = (candies - len(crap) + transfer - 1) / (junior_evolution_cost - 1)
            # keep_for_evo = (candies - keep_for_evo - 1) / (junior_evolution_cost - 1)

            if (candies > 0) and junior_evolution_cost:
                keep_for_evo = int((candies - 1) / junior_evolution_cost)
            else:
                keep_for_evo = 0

            evo_crap = [p for p in crap if p.has_next_evolution() and p.evolution_cost == junior_evolution_cost][:keep_for_evo]

            # If not much to evolve, better keep the candies
            if len(evo_crap) < math.ceil(self.max_pokemon_storage * 0.01):
                evo_crap = []

            transfer = [p for p in crap if p not in evo_crap]
        else:
            evo_crap = []
            transfer = crap

        return (transfer, can_evolve_best, evo_crap, can_upgrade_best)

    def select_upgrades(self, upgrades):
        stardust = self.bot.player_data['currencies'][1]['amount']
        self.logger.info("%d pokemon are selected for a power up", len(upgrades))
        self.logger.info("Stardust available: %d", stardust)

        possible_upgrades = sorted(upgrades, key=lambda u: u[0].cp, reverse=True)
        chosen_upgrades = []

        for pokemon, upgrade_count in possible_upgrades:
            for i in range(upgrade_count):
                candy_cost, stardust_cost = self.get_upgrade_cost(pokemon.level + i * 0.5)
                if stardust < stardust_cost:
                    if i > 0:
                        chosen_upgrades.append((pokemon, i))
                    break
                stardust -= stardust_cost
            else:
                chosen_upgrades.append((pokemon, upgrade_count))

        return chosen_upgrades

    def apply_optimization(self, transfer, evo, upgrade):
        self.logger.info("Transferring %s Pokemon", len(transfer))

        for pokemon in transfer:
            self.transfer_pokemon(pokemon)

        self.logger.info("Powering up %s Pokemon", len(upgrade))

        for pokemon, upgrade_count in upgrade:
            self.upgrade_pokemon(pokemon, upgrade_count)

        if len(evo) == 0:
            return

        if self.config_evolve and self.config_may_use_lucky_egg and (not self.bot.config.test):
            lucky_egg = inventory.items().get(Item.ITEM_LUCKY_EGG.value)  # @UndefinedVariable

            if lucky_egg.count == 0:
                if self.config_evolve_only_with_lucky_egg:
                    self.emit_event("skip_evolve",
                                    formatted="Skipping evolution step. No lucky egg available")
                    return
            elif len(evo) < self.config_evolve_count_for_lucky_egg:
                if self.config_evolve_only_with_lucky_egg:
                    self.emit_event("skip_evolve",
                                    formatted="Skipping evolution step. Not enough Pokemon to evolve with lucky egg: %s/%s" % (len(evo), self.config_evolve_count_for_lucky_egg))
                    return
                elif self.get_pokemon_slot_left() > 5:
                    self.emit_event("skip_evolve",
                                    formatted="Waiting for more Pokemon to evolve with lucky egg: %s/%s" % (len(evo), self.config_evolve_count_for_lucky_egg))
                    return
            else:
                self.use_lucky_egg()

        self.logger.info("Evolving %s Pokemon", len(evo))

        for pokemon in evo:
            self.evolve_pokemon(pokemon)

    def transfer_pokemon(self, pokemon):
        if self.config_transfer and (not self.bot.config.test):
            response_dict = self.bot.api.release_pokemon(pokemon_id=pokemon.unique_id)
        else:
            response_dict = {"responses": {"RELEASE_POKEMON": {"candy_awarded": 0}}}

        if not response_dict:
            return False

        candy_awarded = response_dict.get("responses", {}).get("RELEASE_POKEMON", {}).get("candy_awarded", 0)
        candy = inventory.candies().get(pokemon.pokemon_id)

        if self.config_transfer and (not self.bot.config.test):
            candy.add(candy_awarded)

        self.emit_event("pokemon_release",
                        formatted="Exchanged {pokemon} [IV {iv}] [CP {cp}] [{candy} candies]",
                        data={"pokemon": pokemon.name,
                              "iv": pokemon.iv,
                              "cp": pokemon.cp,
                              "candy": candy.quantity})

        if self.config_transfer and (not self.bot.config.test):
            inventory.pokemons().remove(pokemon.unique_id)

            with self.bot.database as db:
                cursor = db.cursor()
                cursor.execute("SELECT COUNT(name) FROM sqlite_master WHERE type='table' AND name='transfer_log'")

                db_result = cursor.fetchone()

                if db_result[0] == 1:
                    db.execute("INSERT INTO transfer_log (pokemon, iv, cp) VALUES (?, ?, ?)", (pokemon.name, pokemon.iv, pokemon.cp))

            action_delay(self.config_transfer_wait_min, self.config_transfer_wait_max)

        return True

    def use_lucky_egg(self):
        lucky_egg = inventory.items().get(Item.ITEM_LUCKY_EGG.value)  # @UndefinedVariable

        if lucky_egg.count == 0:
            return False

        response_dict = self.bot.use_lucky_egg()

        if not response_dict:
            self.emit_event("lucky_egg_error",
                            level='error',
                            formatted="Failed to use lucky egg!")
            return False

        result = response_dict.get("responses", {}).get("USE_ITEM_XP_BOOST", {}).get("result", 0)

        if result == SUCCESS:
            lucky_egg.remove(1)

            self.emit_event("used_lucky_egg",
                            formatted="Used lucky egg ({amount_left} left).",
                            data={"amount_left": lucky_egg.count})
            return True
        elif result == ERROR_XP_BOOST_ALREADY_ACTIVE:
            self.emit_event("used_lucky_egg",
                            formatted="Lucky egg already active ({amount_left} left).",
                            data={"amount_left": lucky_egg.count})
            return True
        else:
            self.emit_event("lucky_egg_error",
                            level='error',
                            formatted="Failed to use lucky egg!")
            return False

    def evolve_pokemon(self, pokemon):
        if self.config_evolve and (not self.bot.config.test):
            response_dict = self.bot.api.evolve_pokemon(pokemon_id=pokemon.unique_id)
        else:
            response_dict = {"responses": {"EVOLVE_POKEMON": {"result": 1}}}

        if not response_dict:
            return False

        result = response_dict.get("responses", {}).get("EVOLVE_POKEMON", {}).get("result", 0)

        if result != SUCCESS:
            return False

        xp = response_dict.get("responses", {}).get("EVOLVE_POKEMON", {}).get("experience_awarded", 0)
        candy_awarded = response_dict.get("responses", {}).get("EVOLVE_POKEMON", {}).get("candy_awarded", 0)
        candy = inventory.candies().get(pokemon.pokemon_id)
        evolution = response_dict.get("responses", {}).get("EVOLVE_POKEMON", {}).get("evolved_pokemon_data", {})

        if self.config_evolve and (not self.bot.config.test):
            candy.consume(pokemon.evolution_cost - candy_awarded)

        self.emit_event("pokemon_evolved",
                        formatted="Evolved {pokemon} [IV {iv}] [CP {cp}] [{candy} candies] [+{xp} xp]",
                        data={"pokemon": pokemon.name,
                              "iv": pokemon.iv,
                              "cp": pokemon.cp,
                              "candy": candy.quantity,
                              "xp": xp})

        if self.config_evolve and (not self.bot.config.test):
            inventory.pokemons().remove(pokemon.unique_id)

            new_pokemon = inventory.Pokemon(evolution)
            inventory.pokemons().add(new_pokemon)

            with self.bot.database as db:
                cursor = db.cursor()
                cursor.execute("SELECT COUNT(name) FROM sqlite_master WHERE type='table' AND name='evolve_log'")

                db_result = cursor.fetchone()

                if db_result[0] == 1:
                    db.execute("INSERT INTO evolve_log (pokemon, iv, cp) VALUES (?, ?, ?)", (pokemon.name, pokemon.iv, pokemon.cp))

            sleep(self.config_evolve_time)

        return True

    def upgrade_pokemon(self, pokemon, upgrade_count):
        from_level = pokemon.level
        from_cp = pokemon.cp

        total_candy_cost = total_stardust_cost = 0

        for i in range(upgrade_count):
            if self.config_upgrade and (not self.bot.config.test):
                response_dict = self.bot.api.upgrade_pokemon(pokemon_id=pokemon.unique_id)
            else:
                response_dict = {"responses": {"UPGRADE_POKEMON": {"result": 1}}}

            if not response_dict:
                return False

            result = response_dict.get("responses", {}).get("UPGRADE_POKEMON", {}).get("result", 0)
            upgraded_pokemon = response_dict.get("responses", {}).get("UPGRADE_POKEMON", {}).get("upgraded_pokemon", {})

            if result != SUCCESS:
                return False

            candy_cost, stardust_cost = self.get_upgrade_cost(from_level + i * 0.5)
            total_candy_cost += candy_cost
            total_stardust_cost += stardust_cost

        if self.config_upgrade and (not self.bot.config.test):
            inventory.pokemons().remove(pokemon.unique_id)

            new_pokemon = inventory.Pokemon(upgraded_pokemon)
            inventory.pokemons().add(new_pokemon)

            inventory.candies().get(pokemon.pokemon_id).consume(total_candy_cost)
        else:
            new_pokemon = pokemon

        self.emit_event("pokemon_upgraded",
                        formatted="Powered up {pokemon} [IV {iv}] from level {from_level} [CP {from_cp}] to level {to_level} [CP {to_cp}] for {candy} candy and {stardust} stardust",
                        data={"pokemon": pokemon.name,
                              "iv": pokemon.iv,
                              "from_cp": from_cp,
                              "to_cp": new_pokemon.cp,
                              "from_level": from_level,
                              "to_level": new_pokemon.level,
                              "candy": total_candy_cost,
                              "stardust": total_stardust_cost})

        return True

    def use_pokemon_for_xp(self, family_id):
        return inventory.pokemons().name_for(family_id) in self.config_pokemon_for_xp

    UPGRADE_COSTS = {
        1.5: {'dust': 200, 'candy': 1},
        19.5: {'dust': 2500, 'candy': 2},
        2.0: {'dust': 200, 'candy': 1},
        3.0: {'dust': 400, 'candy': 1},
        4.0: {'dust': 400, 'candy': 1},
        5.0: {'dust': 600, 'candy': 1},
        36.5: {'dust': 8000, 'candy': 4},
        1.0: {'dust': 200, 'candy': 1},
        8.0: {'dust': 800, 'candy': 1},
        9.0: {'dust': 1000, 'candy': 1},
        10.0: {'dust': 1000, 'candy': 1},
        11.0: {'dust': 1300, 'candy': 2},
        9.5: {'dust': 1000, 'candy': 1},
        13.0: {'dust': 1600, 'candy': 2},
        14.0: {'dust': 1600, 'candy': 2},
        15.0: {'dust': 1900, 'candy': 2},
        10.5: {'dust': 1000, 'candy': 1},
        4.5: {'dust': 400, 'candy': 1},
        18.0: {'dust': 2200, 'candy': 2},
        19.0: {'dust': 2500, 'candy': 2},
        11.5: {'dust': 1300, 'candy': 2},
        21.0: {'dust': 3000, 'candy': 3},
        22.0: {'dust': 3000, 'candy': 3},
        23.0: {'dust': 3500, 'candy': 3},
        24.0: {'dust': 3500, 'candy': 3},
        25.0: {'dust': 4000, 'candy': 3},
        26.0: {'dust': 4000, 'candy': 3},
        27.0: {'dust': 4500, 'candy': 3},
        28.0: {'dust': 4500, 'candy': 3},
        29.0: {'dust': 5000, 'candy': 3},
        30.0: {'dust': 5000, 'candy': 3},
        8.5: {'dust': 800, 'candy': 1},
        13.5: {'dust': 1600, 'candy': 2},
        6.5: {'dust': 600, 'candy': 1},
        14.5: {'dust': 1600, 'candy': 2},
        35.0: {'dust': 8000, 'candy': 4},
        36.0: {'dust': 8000, 'candy': 4},
        6.0: {'dust': 600, 'candy': 1},
        25.5: {'dust': 4000, 'candy': 3},
        39.0: {'dust': 10000, 'candy': 4},
        40.0: {'dust': 10000, 'candy': 4},
        32.5: {'dust': 6000, 'candy': 4},
        7.0: {'dust': 800, 'candy': 1},
        22.5: {'dust': 3000, 'candy': 3},
        26.5: {'dust': 4000, 'candy': 3},
        15.5: {'dust': 1900, 'candy': 2},
        28.5: {'dust': 4500, 'candy': 3},
        34.5: {'dust': 7000, 'candy': 4},
        30.5: {'dust': 5000, 'candy': 3},
        16.5: {'dust': 1900, 'candy': 2},
        23.5: {'dust': 3500, 'candy': 3},
        32.0: {'dust': 6000, 'candy': 4},
        12.0: {'dust': 1300, 'candy': 2},
        20.5: {'dust': 2500, 'candy': 2},
        18.5: {'dust': 2200, 'candy': 2},
        34.0: {'dust': 7000, 'candy': 4},
        38.5: {'dust': 9000, 'candy': 4},
        3.5: {'dust': 400, 'candy': 1},
        39.5: {'dust': 10000, 'candy': 4},
        37.0: {'dust': 9000, 'candy': 4},
        2.5: {'dust': 200, 'candy': 1},
        37.5: {'dust': 9000, 'candy': 4},
        12.5: {'dust': 1300, 'candy': 2},
        33.5: {'dust': 7000, 'candy': 4},
        33.0: {'dust': 7000, 'candy': 4},
        38.0: {'dust': 9000, 'candy': 4},
        24.5: {'dust': 3500, 'candy': 3},
        17.0: {'dust': 2200, 'candy': 2},
        21.5: {'dust': 3000, 'candy': 3},
        35.5: {'dust': 8000, 'candy': 4},
        17.5: {'dust': 2200, 'candy': 2},
        16.0: {'dust': 1900, 'candy': 2},
        31.0: {'dust': 6000, 'candy': 4},
        27.5: {'dust': 4500, 'candy': 3},
        20.0: {'dust': 2500, 'candy': 2},
        29.5: {'dust': 5000, 'candy': 3},
        40.5: {'dust': 10000, 'candy': 4},
        5.5: {'dust': 600, 'candy': 1},
        31.5: {'dust': 6000, 'candy': 4},
        7.5: {'dust': 800, 'candy': 1}
    }

    def get_upgrade_cost(self, level):
        cost = self.UPGRADE_COSTS[float(level)]
        return cost["candy"], cost["dust"]
