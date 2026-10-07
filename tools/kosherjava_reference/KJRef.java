import com.kosherjava.zmanim.ComprehensiveZmanimCalendar;
import com.kosherjava.zmanim.util.AstronomicalCalculator;
import com.kosherjava.zmanim.util.GeoLocation;
import com.kosherjava.zmanim.util.NOAACalculator;
import com.kosherjava.zmanim.util.SPACalculator;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Supplier;

/** Prints KosherJava zmanim as JSON lines so the Python tests can compare against them. */
public class KJRef {
	record Place(String name, double lat, double lon, double elevation, String zone) {}

	static final Place[] PLACES = {
		new Place("baltimore", 39.3601, -76.7013, 150, "America/New_York"),
		new Place("lakewood", 40.0960, -74.2177, 20, "America/New_York"),
		new Place("jerusalem", 31.7780, 35.2354, 800, "Asia/Jerusalem"),
		new Place("bnei_brak", 32.0840, 34.8338, 30, "Asia/Jerusalem"),
		new Place("london", 51.5074, -0.1278, 20, "Europe/London"),
		new Place("melbourne", -37.8136, 144.9631, 30, "Australia/Melbourne"),
		new Place("quito", -0.1807, -78.4678, 2850, "America/Guayaquil"),
		new Place("tromso", 69.6492, 18.9553, 10, "Europe/Oslo"),
	};

	static final String[] DATES = {
		"2026-01-03", "2026-02-15", "2026-03-20", "2026-04-28", "2026-06-21", "2026-07-05",
		"2026-08-14", "2026-09-22", "2026-10-07", "2026-11-03", "2026-12-21", "2027-05-17",
	};

	public static void main(String[] args) {
		Map<String, AstronomicalCalculator> calcs = new LinkedHashMap<>();
		calcs.put("spa", new SPACalculator());
		calcs.put("noaa", new NOAACalculator());
		for (Map.Entry<String, AstronomicalCalculator> c : calcs.entrySet()) {
			for (Place p : PLACES) {
				GeoLocation loc = new GeoLocation(p.name, p.lat, p.lon, p.elevation, ZoneId.of(p.zone));
				for (String d : DATES) {
					ComprehensiveZmanimCalendar z = new ComprehensiveZmanimCalendar(loc);
					z.setAstronomicalCalculator(c.getValue());
					z.setLocalDate(LocalDate.parse(d));
					StringBuilder sb = new StringBuilder();
					sb.append("{\"calculator\":\"").append(c.getKey()).append("\",\"place\":\"").append(p.name)
						.append("\",\"lat\":").append(p.lat).append(",\"lon\":").append(p.lon)
						.append(",\"elevation\":").append(p.elevation).append(",\"zone\":\"").append(p.zone)
						.append("\",\"date\":\"").append(d).append("\"");
					put(sb, "sea_level_sunrise", z::getSeaLevelSunrise);
					put(sb, "sea_level_sunset", z::getSeaLevelSunset);
					put(sb, "elevation_sunrise", z::getSunrise);
					put(sb, "elevation_sunset", z::getSunset);
					put(sb, "chatzos", z::getChatzosHayom);
					put(sb, "alos_16_1", z::getAlos16Point1Degrees);
					put(sb, "tzais_8_5", z::getTzaisGeonim8Point5Degrees);
					put(sb, "alos_72", z::getAlos72Minutes);
					put(sb, "tzais_72", z::getTzais72Minutes);
					put(sb, "sof_zman_shma_gra", z::getSofZmanShmaGRA);
					put(sb, "sof_zman_shma_mga_72", z::getSofZmanShmaMGA72Minutes);
					put(sb, "sof_zman_tfila_gra", z::getSofZmanTfilaGRA);
					put(sb, "sof_zman_tfila_mga_72", z::getSofZmanTfilaMGA72Minutes);
					put(sb, "mincha_gedola_gra", z::getMinchaGedolaGRA);
					put(sb, "mincha_ketana_gra", z::getMinchaKetanaGRA);
					put(sb, "plag_hamincha_gra", z::getPlagHaminchaGRA);
					put(sb, "candle_lighting", z::getCandleLighting);
					put(sb, "sof_zman_shma_mga_16_1", z::getSofZmanShmaMGA16Point1Degrees);
					put(sb, "sof_zman_tfila_mga_16_1", z::getSofZmanTfilaMGA16Point1Degrees);
					put(sb, "tzais_16_1", z::getTzais16Point1Degrees);
					sb.append("}");
					System.out.println(sb);
				}
			}
		}
	}

	static void put(StringBuilder sb, String key, Supplier<Instant> f) {
		Instant t = f.get();
		sb.append(",\"").append(key).append("\":");
		sb.append(t == null ? "null" : "\"" + t + "\"");
	}
}
