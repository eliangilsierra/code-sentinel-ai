package shop;

import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

@RestController
public class OrderController {

    private final OrderRepository repo; /*?none*/
    private static final String PATTERN = "{not a block} // not a comment"; /*?none*/

    /*<ctor*/ @Autowired
    public OrderController(OrderRepository repo) {
        this.repo = repo; /*?ctor*/
        this.audit = new Audit();
    } /*>ctor*/

    /*<get*/ @GetMapping("/orders/{id}")
    @PreAuthorize("hasRole('USER')")
    public Order get(@PathVariable Long id) {
        Order order = repo.findById(id).orElseThrow(); /*?get*/
        if (order.isHidden()) {
            return null; /*?get*/
        }
        return order;
    } /*>get*/

    /*<convert*/ public <T extends Order, R> Map<String, R> convert(List<T> items, Function<T, R> f)
            throws IOException {
        Map<String, R> out = new HashMap<>();
        for (T item : items) {
            out.put(item.getId(), f.apply(item)); /*?convert*/
        }
        return out;
    } /*>convert*/

    /*<stream*/ public List<String> names(List<Order> orders) {
        return orders.stream()
            .filter(o -> o.isActive())
            .map(o -> {
                String label = o.getName(); /*?stream*/
                return label.trim();
            })
            .toList();
    } /*>stream*/

    /*<schedule*/ public void schedule(Executor executor) {
        executor.execute(new Runnable() {
            @Override
            public void run() {
                audit.log("scheduled"); /*?schedule*/
            }
        });
        executor.shutdown();
    } /*>schedule*/

    /*<listen*/ public void listen(Bus bus) {
        bus.register(new Listener() {
            /*<onEvent*/ @Override
            public void onEvent(Event event) {
                Order order = repo.find(event.id());
                if (order == null) {
                    return;
                }
                order.touch(); /*?onEvent*/
            } /*>onEvent*/
        });
    } /*>listen*/

    /*<init*/ static {
        Registry.register("orders");
        Registry.register("audit");
        Registry.freeze();
        LOG.info("registered"); /*?init*/
    } /*>init*/

    /*<query*/ public String query(String table) {
        String sql = """
            select * from %s where { id } = ?
            // still text
            """.formatted(table);
        char open = '{'; /*?query*/
        return sql + open;
    } /*>query*/

    /*<sync*/ public synchronized void touch(Order order) {
        synchronized (this) {
            order.touch(); /*?sync*/
        }
    } /*>sync*/

    /*<risky*/ public void risky() {
        try {
            repo.flush();
        } catch (IOException e) {
            LOG.error("failed", e); /*?risky*/
        } finally {
            repo.close();
        }
    } /*>risky*/

    /*<mapped*/ @RequestMapping({"/a", "/b"})
    public String mapped() {
        String value = "mapped";
        return value; /*?mapped*/
    } /*>mapped*/

    public static class Audit {
        private int count; /*?none*/

        /*<log*/ public void log(String message) {
            count++;
            LOG.info(message); /*?log*/
            LOG.info("count " + count);
            LOG.info("done");
        } /*>log*/
    }

    public interface Listener {
        /*<greet*/ default String greet(String name) {
            String prefix = "hello";
            String suffix = "!";
            String text = prefix + name;
            return text + suffix; /*?greet*/
        } /*>greet*/
    }

    public enum Status {
        ACTIVE {
            /*<open*/ public boolean open() { return true; /*?open*/ } /*>open*/
        },
        CLOSED;

        /*<label*/ public String label() {
            String base = name().toLowerCase();
            String first = base.substring(0, 1).toUpperCase();
            String rest = base.substring(1);
            return first + rest; /*?label*/
        } /*>label*/
    }

    public record Point(int x, int y) {
        /*<compact*/ Point {
            if (x < 0) {
                throw new IllegalArgumentException("x");
            }
            if (y < 0) {
                throw new IllegalArgumentException("y"); /*?compact*/
            }
        } /*>compact*/
    }
}
